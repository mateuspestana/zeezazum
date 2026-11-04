"""Extrai contas do Instagram e do TikTok de uma coluna de texto livre com redes sociais.

O texto de origem é sujo por natureza: várias URLs separadas por `;`, prefixos
como "Link Instagram:", maiúsculas/minúsculas variadas, às vezes sem protocolo
ou sem `www`, e às vezes só um handle avulso ("INSTAGRAM: @fulano"). As funções
aqui só assumem que os itens estão separados por `;` ou quebra de linha — esse
formato foi confirmado direto na amostra real do `candidatos_atual.parquet`.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from urllib.parse import parse_qs, quote, quote_plus, unquote, unquote_plus, urlparse

from zeezazum.models import SocialAccount

logger = logging.getLogger(__name__)

_SPLIT_RE = re.compile(r"[;\n]+")
# inclui "-" e "@" no meio do path: "-" aparece em handles digitados errado mas
# reais (ex.: "instagram.com/pr-fulano"), e "@" aparece em "instagram.com/@fulano"
# ou até colado sem separador ("instagram.com/xx@fulano") — tratado em _clean_handle.
_URL_HANDLE_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.@-]+)", re.IGNORECASE)
_LABEL_RE = re.compile(
    r"^(?:link\s+)?(?:instagram|insta|ig)\s*:?\s*@?([A-Za-z0-9_.]+)$",
    re.IGNORECASE,
)
# segmentos de path que não são handles de perfil (URLs de post/reel/etc.)
_NON_PROFILE_SEGMENTS = {"", "p", "reel", "reels", "stories", "explore", "tv", "accounts"}


def _clean_handle(raw: str) -> str | None:
    handle = raw.strip().strip("/.,")
    handle = handle.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if "@" in handle:
        # "instagram.com/@fulano" ou lixo colado antes do "@" (ex.: "la@lavinia...")
        # — o handle real é o que vem depois do último "@".
        handle = handle.rsplit("@", 1)[-1]
    if not handle or handle.lower() in _NON_PROFILE_SEGMENTS:
        return None
    return handle


def extract_instagram_accounts(row_id: str, text: str | None) -> list[SocialAccount]:
    """Retorna as contas de Instagram (deduplicadas) mencionadas em `text`."""
    if not text:
        return []

    found: dict[str, SocialAccount] = {}
    for fragment in _SPLIT_RE.split(text):
        fragment = fragment.strip()
        if not fragment:
            continue

        handle: str | None = None
        url_match = _URL_HANDLE_RE.search(fragment)
        if url_match:
            handle = _clean_handle(url_match.group(1))
        else:
            label_match = _LABEL_RE.match(fragment)
            if label_match:
                handle = _clean_handle(label_match.group(1))

        if not handle:
            continue

        key = handle.lower()
        if key not in found:
            found[key] = SocialAccount(
                row_id=row_id,
                platform="instagram",
                handle=key,
                url=f"https://www.instagram.com/{key}/",
            )

    return list(found.values())


# TikTok: handles têm letras, dígitos, "_" e "." (nunca terminam em ".").
_TT_URL_HANDLE_RE = re.compile(r"tiktok\.com/@([A-Za-z0-9_.]+)", re.IGNORECASE)
# o separador (":", espaço ou "@") é obrigatório: "ttfulano" não é o @fulano
_TT_LABEL_RE = re.compile(
    r"^(?:link\s+)?(?:tik\s*tok|tt)(?:\s*:\s*@?|\s+@?|\s*@)([A-Za-z0-9_.]+)$",
    re.IGNORECASE,
)
# links curtos só resolvem com uma requisição (redirect), que o `prepare` não faz
_TT_SHORT_LINK_RE = re.compile(
    r"(?:vm|vt)\.tiktok\.com/|tiktok\.com/t/", re.IGNORECASE
)


def extract_tiktok_accounts(row_id: str, text: str | None) -> list[SocialAccount]:
    """Retorna as contas de TikTok (deduplicadas) mencionadas em `text`. Aceita
    `tiktok.com/@handle`, `tiktok.com/@handle/video/<id>` e rótulos como
    "TikTok: @handle". Links curtos (`vm.tiktok.com/...`) não dão para resolver
    offline: são ignorados com um aviso no log."""
    if not text:
        return []

    found: dict[str, SocialAccount] = {}
    for fragment in _SPLIT_RE.split(text):
        fragment = fragment.strip()
        if not fragment:
            continue

        handle: str | None = None
        url_match = _TT_URL_HANDLE_RE.search(fragment)
        if url_match:
            handle = url_match.group(1)
        elif _TT_SHORT_LINK_RE.search(fragment):
            logger.warning(
                "Linha %s: link curto do TikTok não resolvível offline, ignorado: %s",
                row_id,
                fragment,
            )
            continue
        else:
            label_match = _TT_LABEL_RE.match(fragment)
            if label_match:
                handle = label_match.group(1)

        handle = handle.strip(".") if handle else None
        if not handle:
            continue

        key = handle.lower()
        if key not in found:
            found[key] = SocialAccount(
                row_id=row_id,
                platform="tiktok",
                handle=key,
                url=f"https://www.tiktok.com/@{key}",
            )

    return list(found.values())


# Hashtags do TikTok: `tiktok.com/tag/<nome>` (o nome pode vir codificado) ou `#nome`/`nome`.
_TAG_URL_RE = re.compile(r"tiktok\.com/tag/([^/?#\s]+)", re.IGNORECASE)
_TAG_WORD_RE = re.compile(r"^#?(\w+)$", re.UNICODE)
_TAG_SPLIT_RE = re.compile(r"[;,\s]+")


def extract_tiktok_tags(row_id: str, text: str | None) -> list[SocialAccount]:
    """Retorna as hashtags (deduplicadas) de `text`, como contas da plataforma
    `tiktok_tag`. Aceita `tiktok.com/tag/bolsonaro`, `#bolsonaro` e a palavra solta,
    separadas por espaço, vírgula, ponto e vírgula ou quebra de linha. O nome é
    normalizado para minúsculas (o TikTok não diferencia)."""
    if not text:
        return []

    found: dict[str, SocialAccount] = {}
    for token in _TAG_SPLIT_RE.split(unicodedata.normalize("NFC", text)):
        if not token:
            continue
        url_match = _TAG_URL_RE.search(token)
        if url_match:
            name = unquote(url_match.group(1))
        else:
            word = _TAG_WORD_RE.match(token)
            if not word or "tiktok" in token.lower():
                continue  # link de perfil/vídeo ou texto que não é hashtag
            name = word.group(1)
        name = name.lstrip("#").lower()
        if name and name not in found:
            found[name] = SocialAccount(
                row_id=row_id,
                platform="tiktok_tag",
                handle=name,
                url=f"https://www.tiktok.com/tag/{quote(name)}",
            )
    return list(found.values())


# Buscas do TikTok: `tiktok.com/search/video?q=...`, `tiktok.com/search?q=...`,
# `tiktok.com/search/jair+bolsonaro` ou texto simples (uma busca por linha ou por `;`).
_SEARCH_URL_RE = re.compile(r"(?:https?://)?(?:www\.)?tiktok\.com/search\S*", re.IGNORECASE)
_SEARCH_TABS = {"video", "user", "live", "photo", "top", "playlist"}
_SEARCH_SPLIT_RE = re.compile(r"[;\n]+")


def _search_handle(query: str) -> str:
    """Identificador de pasta da busca: minúsculas, espaços como `+`, e `%XX` nos
    caracteres que quebrariam caminho ou URL (`jair bolsonaro` -> `jair+bolsonaro`)."""
    query = re.sub(r"\s+", " ", unicodedata.normalize("NFC", query)).strip().lower()
    for char in '%/\\?#&=+"':
        query = query.replace(char, f"%{ord(char):02X}")
    return query.replace(" ", "+")


def _query_from_search_url(fragment: str) -> str | None:
    match = _SEARCH_URL_RE.search(fragment)
    if not match:
        return None
    url = match.group(0)
    if not url.lower().startswith("http"):
        url = "https://" + url
    parsed = urlparse(url)
    query = (parse_qs(parsed.query).get("q") or [None])[0]
    if query is None:
        segments = [seg for seg in parsed.path.split("/") if seg]
        # /search/<texto> ou /search/<aba>/<texto>; sem texto não há o que buscar
        rest = [seg for seg in segments[1:] if seg.lower() not in _SEARCH_TABS]
        query = unquote_plus(rest[0]) if rest else None
    return query.strip() if query and query.strip() else None


def extract_tiktok_searches(row_id: str, text: str | None) -> list[SocialAccount]:
    """Retorna as buscas do TikTok (deduplicadas) de `text`, como contas da plataforma
    `tiktok_search`. Cada busca é uma linha ou um trecho separado por `;`; links de
    perfil, vídeo ou tag no meio do texto são ignorados."""
    if not text:
        return []

    found: dict[str, SocialAccount] = {}
    for fragment in _SEARCH_SPLIT_RE.split(text):
        fragment = fragment.strip()
        if not fragment:
            continue
        if "tiktok.com" in fragment.lower():
            query = _query_from_search_url(fragment)
        else:
            query = fragment
        if not query:
            continue
        handle = _search_handle(query)
        if handle and handle not in found:
            found[handle] = SocialAccount(
                row_id=row_id,
                platform="tiktok_search",
                handle=handle,
                url=f"https://www.tiktok.com/search/video?q={quote_plus(unquote_plus(handle))}",
            )
    return list(found.values())


def extract_accounts(platform: str, row_id: str, text: str | None) -> list[SocialAccount]:
    """Despacha para o extrator da plataforma."""
    if platform == "instagram":
        return extract_instagram_accounts(row_id, text)
    if platform == "tiktok":
        return extract_tiktok_accounts(row_id, text)
    if platform == "tiktok_tag":
        return extract_tiktok_tags(row_id, text)
    if platform == "tiktok_search":
        return extract_tiktok_searches(row_id, text)
    raise ValueError(f"Plataforma não suportada: {platform}")
