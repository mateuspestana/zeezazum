"""Converte entradas do yt-dlp (listagem `extract_flat` de um perfil do TikTok) em `Post`s.

O TikTok bloqueia o navegador anônimo na lista de vídeos (`/api/post/item_list/`
responde 200 com corpo vazio), então a listagem vem do yt-dlp, sem login. Cada
entrada já traz o que precisamos: id, legenda, `timestamp`, contagens, capa e
trilha. Campos confirmados contra a listagem real de @tiktok em 2026-11.

Mídia:
- vídeo (`/video/<id>`): o `MediaItem.url` guarda a URL da *página* do post, que não
  expira — o download é feito pelo yt-dlp (ver `media.py`), que resolve o arquivo na
  hora. As URLs diretas do CDN expiram em horas e dependem de cabeçalhos.
- post de fotos/slideshow: o yt-dlp não baixa o carrossel, então só a capa é
  enfileirada (nada que faça `media-sync` falhar para sempre). A listagem monta toda
  URL como `/video/<id>`, mas slideshows chegam sem `duration` (o yt-dlp descarta a
  duração 0 do vídeo deles) — esse é o sinal usado. Inferido da leitura do código do
  yt-dlp; não verificado contra um slideshow real.
- capa: URL assinada do CDN (expira em ~2 dias); é o último item do post.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from zeezazum.models import MediaItem, MediaKind, Post

_HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)
_MENTION_RE = re.compile(r"(?<![\w.])@([A-Za-z0-9_.]+)")
_POST_URL_RE = re.compile(r"https?://[^/]*tiktok\.com/@[^/]+/(video|photo)/(\d+)")
# ordem de preferência da capa: a original tem a maior resolução
_COVER_PREFERENCE = ("originCover", "cover", "dynamicCover")


def post_kind(url: str, duration=None) -> str:
    """"video" ou "photo" (slideshow). Uma URL `/photo/` já é foto; uma `/video/` sem
    duração também (ver docstring do módulo)."""
    match = _POST_URL_RE.search(url or "")
    if match and match.group(1) == "photo":
        return "photo"
    return "video" if duration else "photo"


def _cover_url(entry: dict) -> str | None:
    thumbnails = [t for t in entry.get("thumbnails") or [] if isinstance(t, dict) and t.get("url")]
    for wanted in _COVER_PREFERENCE:
        for thumb in thumbnails:
            if thumb.get("id") == wanted:
                return thumb["url"]
    return thumbnails[0]["url"] if thumbnails else None


def _int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def parse_entry(entry: dict) -> Post | None:
    """Uma entrada da listagem -> `Post`. Devolve None para o que não é post de perfil
    (sem id numérico ou sem URL de vídeo/foto)."""
    if not isinstance(entry, dict):
        return None
    url = entry.get("url") or entry.get("webpage_url") or ""
    match = _POST_URL_RE.search(url)
    if not match:
        return None
    post_id = match.group(2)
    post_url = match.group(0)
    kind = post_kind(url, entry.get("duration"))

    media: list[MediaItem] = []
    if kind == "video":
        media.append(MediaItem(kind=MediaKind.VIDEO, url=post_url, index=0))
    cover = _cover_url(entry)
    if cover:
        media.append(MediaItem(kind=MediaKind.IMAGE, url=cover, index=len(media)))

    timestamp = entry.get("timestamp")
    caption = entry.get("description") or entry.get("title") or None
    text = caption or ""
    artists = entry.get("artists")
    return Post(
        shortcode=post_id,
        post_url=post_url,
        timestamp=(
            datetime.fromtimestamp(timestamp, tz=timezone.utc)
            if isinstance(timestamp, (int, float)) and timestamp > 0
            else None
        ),
        caption=caption,
        media=tuple(media),
        raw=entry,
        metrics=extract_metrics(entry, kind, text, artists),
    )


def extract_metrics(entry: dict, kind: str, text: str, artists) -> dict:
    """Campos planos para análise. Ausência vira None (nunca erro)."""
    return {
        "like_count": _int(entry.get("like_count")),
        "comment_count": _int(entry.get("comment_count")),
        "view_count": _int(entry.get("view_count")),
        "share_count": _int(entry.get("repost_count")),
        "save_count": _int(entry.get("save_count")),
        "video_duration": entry.get("duration"),
        "media_type_label": "video" if kind == "video" else "photo",
        "hashtags": _HASHTAG_RE.findall(text),
        "mentions": _MENTION_RE.findall(text),
        "audio_title": entry.get("track"),
        "audio_artist": ", ".join(artists) if isinstance(artists, list) else artists,
        "owner_username": entry.get("uploader"),
        "owner_id": entry.get("channel_id"),
    }


def parse_user_detail(udr: dict) -> dict | None:
    """`webapp.user-detail` do bloco `__UNIVERSAL_DATA_FOR_REHYDRATION__` -> dict com
    status e estatísticas do perfil, ou None se o bloco não tiver o formato esperado.

    `status_code` 0 = existe; 10221 = inexistente/banido (confirmado em 2026-11)."""
    detail = (udr.get("__DEFAULT_SCOPE__") or {}).get("webapp.user-detail")
    if not isinstance(detail, dict):
        return None
    info = detail.get("userInfo") or {}
    user = info.get("user") or {}
    stats = info.get("stats") or {}
    return {
        "status_code": detail.get("statusCode"),
        "private": bool(user.get("privateAccount")),
        "sec_uid": user.get("secUid"),
        "followers": _int(stats.get("followerCount")),
        "following": _int(stats.get("followingCount")),
        "posts_total": _int(stats.get("videoCount")),
    }


def _author(item: dict) -> tuple[str | None, str | None]:
    """(uniqueId, id numérico) do autor; o campo vem como dict ou, em alguns payloads,
    como o próprio uniqueId."""
    author = item.get("author")
    if isinstance(author, dict):
        return author.get("uniqueId"), author.get("id") or item.get("authorId")
    return (author if isinstance(author, str) else None), item.get("authorId")


def parse_item(item: dict) -> Post | None:
    """Item da API web (`/api/challenge/item_list/`: lista de vídeos de uma hashtag)
    -> `Post`. É o mesmo formato que o Zeeschuimer captura. Devolve None para o que
    não é post (sem id/autor) e para lives (`liveRoomInfo`).

    Mapeamento conforme o `modules/tiktok.js` do Zeeschuimer e a API web; NÃO
    verificado contra uma resposta real (o TikTok só entrega essa lista com a sessão
    do captcha resolvido, e nenhuma sessão assim estava disponível ao escrever)."""
    if not isinstance(item, dict) or "liveRoomInfo" in item or item.get("isAd"):
        return None  # lives e anúncios (a busca pode misturar `isAd`)
    post_id = str(item.get("id") or "")
    username, owner_id = _author(item)
    if not post_id.isdigit() or not username:
        return None

    image_post = item.get("imagePost") or {}
    images = [
        urls[0]
        for image in image_post.get("images") or []
        if isinstance(image, dict)
        and (urls := (image.get("imageURL") or {}).get("urlList"))
    ]
    is_photo = bool(image_post)
    kind = "photo" if is_photo else "video"
    post_url = f"https://www.tiktok.com/@{username}/{'photo' if is_photo else 'video'}/{post_id}"

    video = item.get("video") or {}
    media: list[MediaItem] = []
    if is_photo:
        media = [MediaItem(kind=MediaKind.IMAGE, url=u, index=i) for i, u in enumerate(images)]
    else:
        media.append(MediaItem(kind=MediaKind.VIDEO, url=post_url, index=0))
        cover = next(
            (video[k] for k in ("originCover", "cover", "dynamicCover") if video.get(k)), None
        )
        if cover:
            media.append(MediaItem(kind=MediaKind.IMAGE, url=cover, index=1))

    caption = item.get("desc") or None
    text = caption or ""
    stats = item.get("stats") or {}
    music = item.get("music") or {}
    created = item.get("createTime")
    try:
        created = int(created)
    except (TypeError, ValueError):
        created = None
    hashtags = [
        t["hashtagName"]
        for t in item.get("textExtra") or []
        if isinstance(t, dict) and t.get("hashtagName")
    ] or _HASHTAG_RE.findall(text)
    return Post(
        shortcode=post_id,
        post_url=post_url,
        timestamp=datetime.fromtimestamp(created, tz=timezone.utc) if created else None,
        caption=caption,
        media=tuple(media),
        raw=item,
        metrics={
            "like_count": _int(stats.get("diggCount")),
            "comment_count": _int(stats.get("commentCount")),
            "view_count": _int(stats.get("playCount")),
            "share_count": _int(stats.get("shareCount")),
            "save_count": _int(stats.get("collectCount")),
            "video_duration": video.get("duration") if not is_photo else None,
            "media_type_label": kind,
            "hashtags": hashtags,
            "mentions": _MENTION_RE.findall(text),
            "audio_title": music.get("title"),
            "audio_artist": music.get("authorName"),
            "owner_username": username,
            "owner_id": owner_id,
        },
    )
