"""Converte JSON capturado do tráfego de rede do Instagram em `Post`s.

Campos confirmados batendo com o mapper real do Zeeschuimer
(digitalmethodsinitiative/zeeschuimer, modules/instagram.js) e contra tráfego
real capturado em 2026-09: `code` (não `shortcode` — esse só existe no shape
GraphQL antigo), `taken_at`, `image_versions2.candidates[0].url`,
`video_versions[0].url`, `carousel_media`, `media_type` (1=foto, 2=vídeo,
8=carrossel).

A extração é em duas camadas, também espelhando a estratégia do Zeeschuimer:

1. **Precisa**: procura a conexão nomeada
   `xdt_api__v1__feed__user_timeline_graphql_connection` (a timeline de posts
   do próprio perfil) e extrai só os nós de lá. O Instagram também carrega em
   segundo plano, na mesma página, respostas de GraphQL com conteúdo
   pré-buscado que nunca é exibido (sugestões, outros perfis) — usar essa
   conexão nomeada evita capturar esse lixo por engano.
2. **Genérica (fallback)**: se essa conexão não aparecer no payload (schema
   mudou de novo), cai pra um scan recursivo por qualquer nó com a forma de
   post — menos preciso, mas resiliente a mudanças futuras da API.

Em ambas, itens de anúncio (`product_type == "ad"`, `ad_action` presente, ou
link de redirect de ads do Facebook) são descartados — mesmo filtro que o
Zeeschuimer aplica, porque esse tipo de conteúdo é servido em segundo plano
mesmo sem aparecer de fato no feed.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterator

from zeezazum.models import MediaItem, MediaKind, Post

logger = logging.getLogger(__name__)

# `__typename` do shape Polaris (mesmo mapeamento usado pelo Zeeschuimer/4CAT) quando
# `media_type` numérico não vem no nó.
_POLARIS_MEDIA_TYPE = {
    "XIGPolarisPhotoMedia": 1,
    "XIGPolarisVideoMedia": 2,
    "XIGPolarisCarouselMedia": 8,
}

_TIMELINE_CONNECTION_KEYS = (
    "xdt_api__v1__feed__user_timeline_graphql_connection",
    "xdt_location_get_web_info_tab",
)


def _is_ad(node: dict) -> bool:
    if node.get("product_type") == "ad":
        return True
    if node.get("ad_action") is not None:
        return True
    link = node.get("link")
    if isinstance(link, str) and link.startswith("https://www.facebook.com/ads/"):
        return True
    return False


def _iter_timeline_connection_nodes(obj: Any) -> Iterator[dict]:
    """Extração precisa: só a timeline de posts do perfil, ignorando conteúdo
    pré-buscado em segundo plano que compartilha o mesmo endpoint de resposta."""
    if isinstance(obj, dict):
        for key in _TIMELINE_CONNECTION_KEYS:
            connection = obj.get(key)
            if isinstance(connection, dict):
                for edge in connection.get("edges") or []:
                    node = edge.get("node") if isinstance(edge, dict) else None
                    if node and node.get("user") and not _is_ad(node):
                        yield node
        for value in obj.values():
            yield from _iter_timeline_connection_nodes(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_timeline_connection_nodes(item)


def _belongs_to(node: dict, handle: str) -> bool:
    """O nó é do perfil raspado? Aceita autor/owner igual ao handle ou coautoria
    (posts collab aparecem na grade de ambos)."""
    wanted = handle.lower()
    for key in ("user", "owner"):
        author = node.get(key)
        if isinstance(author, dict) and (author.get("username") or "").lower() == wanted:
            return True
    for coauthor in node.get("coauthor_producers") or []:
        if isinstance(coauthor, dict) and (coauthor.get("username") or "").lower() == wanted:
            return True
    return False


def _iter_post_nodes_generic(obj: Any) -> Iterator[dict]:
    """Fallback tolerante: qualquer nó que "pareça" um post, usado só quando a
    conexão nomeada acima não é encontrada no payload."""
    if isinstance(obj, dict):
        has_id = "shortcode" in obj or "code" in obj
        has_media = (
            "display_url" in obj
            or "display_uri" in obj
            or "image_versions2" in obj
            or "video_url" in obj
            or "video_versions" in obj
            or "carousel_media" in obj
            or "edge_sidecar_to_children" in obj
        )
        has_author = "user" in obj or "owner" in obj  # GraphQL antigo usa "owner"
        if has_id and has_media and has_author and not _is_ad(obj):
            yield obj
        for value in obj.values():
            yield from _iter_post_nodes_generic(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_post_nodes_generic(item)


def _extract_timestamp(node: dict) -> datetime | None:
    for key in ("taken_at", "taken_at_timestamp", "device_timestamp"):
        value = node.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return datetime.fromtimestamp(value, tz=timezone.utc)
    return None


def _extract_caption(node: dict) -> str | None:
    caption = node.get("caption")
    if isinstance(caption, dict):
        text = caption.get("text")
        if text:
            return text
    edge_caption = node.get("edge_media_to_caption")
    if isinstance(edge_caption, dict):
        edges = edge_caption.get("edges") or []
        if edges:
            return edges[0].get("node", {}).get("text")
    return None


def _best_video_url(node: dict) -> str | None:
    video_url = node.get("video_url")
    if video_url:
        return video_url

    versions = node.get("video_versions")
    if versions:
        # nem sempre vem ordenado — pega a de maior largura disponível
        return max(versions, key=lambda v: v.get("width", 0))["url"]

    return None


def _best_media_from_node(node: dict, index: int) -> MediaItem | None:
    video_url = _best_video_url(node)
    if video_url:
        return MediaItem(kind=MediaKind.VIDEO, url=video_url, index=index)

    candidates = (node.get("image_versions2") or {}).get("candidates")
    if candidates:
        # candidatas costumam vir ordenadas da maior para a menor resolução
        return MediaItem(kind=MediaKind.IMAGE, url=candidates[0]["url"], index=index)

    display_url = node.get("display_url") or node.get("display_uri")
    if display_url:
        return MediaItem(kind=MediaKind.IMAGE, url=display_url, index=index)

    return None


def _extract_media(node: dict) -> list[MediaItem]:
    carousel = node.get("carousel_media")
    if not carousel:
        sidecar = node.get("edge_sidecar_to_children")
        if isinstance(sidecar, dict):
            carousel = [edge.get("node", edge) for edge in sidecar.get("edges", [])]

    if carousel:
        media = [m for i, child in enumerate(carousel) if (m := _best_media_from_node(child, i))]
        if media:
            return media

    single = _best_media_from_node(node, 0)
    return [single] if single else []


_HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)
_MENTION_RE = re.compile(r"(?<![\w.])@([A-Za-z0-9_.]+)")
_PRODUCT_TYPE_LABEL = {1: "photo", 2: "video", 8: "carousel"}


def _first_int(node: dict, *keys: str) -> int | None:
    for key in keys:
        value = node.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return None


def _is_pinned(node: dict) -> bool:
    if node.get("is_pinned") is True:
        return True
    pinned = node.get("timeline_pinned_user_ids")
    if isinstance(pinned, list) and pinned:
        return True
    return bool(node.get("pinned_info") or node.get("profile_grid_thumbnail_fitting_style") == "pinned")


def extract_metrics(node: dict, caption: str | None) -> dict:
    """Campos planos úteis para análise a partir do nó bruto. Tudo opcional: o
    Instagram muda o schema com frequência, então ausência vira None (nunca erro).
    Curtidas ocultas pelo autor aparecem como like_and_view_counts_disabled=True."""
    location = node.get("location") if isinstance(node.get("location"), dict) else {}
    usertags = (node.get("usertags") or {}).get("in") or []
    coauthors = node.get("coauthor_producers") or []
    clips = node.get("clips_metadata") if isinstance(node.get("clips_metadata"), dict) else {}
    music_info = (clips.get("music_info") or {}).get("music_asset_info") or {}
    original_sound = (clips.get("original_sound_info") or {})
    media_type = node.get("media_type") or _POLARIS_MEDIA_TYPE.get(node.get("__typename"))
    text = caption or ""
    return {
        "like_count": _first_int(node, "like_count"),
        "comment_count": _first_int(node, "comment_count"),
        "play_count": _first_int(node, "play_count", "ig_play_count"),
        "view_count": _first_int(node, "view_count", "video_view_count"),
        "media_type": media_type,
        "media_type_label": _PRODUCT_TYPE_LABEL.get(media_type),
        "product_type": node.get("product_type"),
        "video_duration": node.get("video_duration"),
        "n_carousel": node.get("carousel_media_count"),
        "location_name": location.get("name"),
        "location_id": location.get("pk") or location.get("id"),
        "usertags": [t.get("user", {}).get("username") for t in usertags if isinstance(t, dict)],
        "coauthors": [c.get("username") for c in coauthors if isinstance(c, dict)],
        "accessibility_caption": node.get("accessibility_caption"),
        "audio_title": music_info.get("title") or original_sound.get("original_audio_title"),
        "audio_artist": music_info.get("display_artist") or (original_sound.get("ig_artist") or {}).get("username"),
        "comments_disabled": node.get("comments_disabled"),
        "likes_hidden": node.get("like_and_view_counts_disabled"),
        "hashtags": _HASHTAG_RE.findall(text),
        "mentions": _MENTION_RE.findall(text),
        "owner_username": (node.get("user") or node.get("owner") or {}).get("username"),
    }


def parse_post_node(node: dict) -> Post | None:
    shortcode = node.get("shortcode") or node.get("code")
    if not shortcode:
        return None
    caption = _extract_caption(node)
    return Post(
        shortcode=shortcode,
        post_url=f"https://www.instagram.com/p/{shortcode}/",
        timestamp=_extract_timestamp(node),
        caption=caption,
        media=tuple(_extract_media(node)),
        raw=node,
        metrics=extract_metrics(node, caption),
        is_pinned=_is_pinned(node),
    )


def parse_posts_from_payload(payload: Any, handle: str | None = None) -> list[Post]:
    """Extrai todos os posts únicos (por shortcode/code) encontrados em um payload JSON.

    Tenta primeiro a extração precisa (conexão de timeline nomeada); só cai pro
    scan genérico se essa conexão não existir no payload — assim evitamos tanto
    capturar conteúdo pré-buscado indevido quanto perder posts se o Instagram
    trocar de shape de novo.

    Com `handle`, os nós do scan genérico são filtrados pelo dono do perfil (ou
    coautoria): o Instagram serve conteúdo pré-buscado de outros perfis pelo mesmo
    endpoint e nada na resposta o distingue, só a página aberta.
    """
    precise_nodes = list(_iter_timeline_connection_nodes(payload))
    if precise_nodes:
        nodes = precise_nodes
    else:
        nodes = list(_iter_post_nodes_generic(payload))
        if handle:
            kept = [n for n in nodes if _belongs_to(n, handle)]
            if len(kept) != len(nodes):
                logger.info(
                    "Fallback genérico: %d nó(s) de outros perfis descartado(s) em @%s",
                    len(nodes) - len(kept),
                    handle,
                )
            nodes = kept

    posts: dict[str, Post] = {}
    for node in nodes:
        post = parse_post_node(node)
        if post and post.shortcode not in posts:
            posts[post.shortcode] = post
    return list(posts.values())
