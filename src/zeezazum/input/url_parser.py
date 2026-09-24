"""Extrai contas do Instagram de uma coluna de texto livre com redes sociais.

O texto de origem é sujo por natureza: várias URLs separadas por `;`, prefixos
como "Link Instagram:", maiúsculas/minúsculas variadas, às vezes sem protocolo
ou sem `www`, e às vezes só um handle avulso ("INSTAGRAM: @fulano"). As funções
aqui só assumem que os itens estão separados por `;` ou quebra de linha — esse
formato foi confirmado direto na amostra real do `candidatos_atual.parquet`.
"""
from __future__ import annotations

import re

from zeezazum.models import SocialAccount

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
