"""Sessão do TikTok resolvida à mão.

O TikTok mostra um captcha de deslizar o quebra-cabeça a navegadores anônimos (medido
em 2026-11: em 6 de 6 aberturas do perfil, mesmo quando os dados do perfil chegam).
Não automatizamos a resolução: quando a coleta é bloqueada, o usuário resolve o captcha
UMA vez numa janela visível (`zeezazum tiktok-session`) e a sessão (cookies) é reaproveitada
pelo navegador e pelo yt-dlp nas execuções seguintes.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

DEFAULT_SESSION_FILE = "sessions/tiktok_storage_state.json"

# Há um captcha visível na página? O modal do TikTok usa `.captcha-verify-container`.
# `getClientRects` (e não `offsetParent`) porque o modal é `position: fixed`.
CAPTCHA_JS = """() => {
  const el = document.querySelector('.captcha-verify-container, #captcha-verify-container-main-page');
  return !!el && el.getClientRects().length > 0;
}"""

_DEFAULT_URL = "https://www.tiktok.com/@tiktok"


def storage_state_to_netscape(state: dict) -> str:
    """Converte o `storage_state` do Playwright no formato de cookies do yt-dlp (Netscape)."""
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in state.get("cookies", []):
        domain = cookie.get("domain", "")
        if "tiktok" not in domain:
            continue  # o yt-dlp só precisa dos cookies do próprio TikTok
        expires = cookie.get("expires", -1)
        fields = [
            domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            cookie.get("path", "/"),
            "TRUE" if cookie.get("secure") else "FALSE",
            str(int(expires)) if expires and expires > 0 else "0",
            cookie["name"],
            cookie["value"],
        ]
        prefix = "#HttpOnly_" if cookie.get("httpOnly") else ""
        lines.append(prefix + "\t".join(fields))
    return "\n".join(lines) + "\n"


# Links de vídeo visíveis na página. Prova de que o captcha foi RESOLVIDO: fechar o modal
# pelo X também o faz sumir, mas a página segue sem vídeos (medido: acesso anônimo = 0).
VIDEOS_JS = "() => document.querySelectorAll('a[href*=\"/video/\"]').length"


def is_solved(page) -> bool:
    """Sem captcha na tela e com vídeos carregados."""
    try:
        return not page.evaluate(CAPTCHA_JS) and page.evaluate(VIDEOS_JS) > 0
    except Exception:  # página recarregando ou janela fechada
        return False


def wait_until_solved(
    page, *, timeout: float = 300.0, poll: float = 1.0, echo=print,
    sleep=time.sleep, clock=time.monotonic,
) -> bool:
    """Espera, sem teclado, o captcha ser resolvido: usado quando não há terminal
    interativo (ex.: `! comando` no Claude Code). Resolvido = sem captcha e com vídeos
    na página por 3 verificações seguidas. Devolve False se a janela foi fechada ou o
    tempo (`timeout`) esgotou: nesse caso nada deve ser salvo."""
    start, solved = clock(), 0
    while clock() - start < timeout:
        if is_solved(page):
            solved += 1
            if solved >= 3:
                return True
        else:
            solved = 0
            try:
                if page.is_closed():
                    return False
            except Exception:
                return False
        sleep(poll)
    echo("Tempo esgotado esperando o captcha ser resolvido.")
    return False


def capture_session(
    output: Path,
    *,
    browser_name: str = "chromium",
    urls: tuple[str, ...] | list[str] = (_DEFAULT_URL,),
    wait=None,
    echo=print,
) -> bool:
    """Abre uma janela visível em cada URL (perfil, tag ou busca), na mesma sessão, e
    espera o usuário resolver o captcha de cada uma. Com terminal interativo espera o
    Enter; sem ele, observa a página. Só grava `output` se TODAS as páginas terminaram
    com vídeos na tela: uma sessão sem captcha resolvido não substitui a existente.
    Devolve True se gravou."""
    from playwright.sync_api import sync_playwright

    output.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = getattr(pw, browser_name).launch(headless=False)
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            for url in urls:
                page.goto(url, wait_until="domcontentloaded")
                echo(
                    f"\nNa janela que abriu ({url}), deslize o quebra-cabeça do captcha até "
                    "encaixar e espere os vídeos aparecerem. Não feche o modal pelo X."
                )
                if wait is None and sys.stdin.isatty():
                    wait = input
                if wait is not None:
                    wait("Quando os vídeos aparecerem, aperte Enter aqui... ")
                    ok = is_solved(page)
                else:
                    echo("Sem terminal interativo: salvo sozinho quando os vídeos aparecerem (até 5 min).")
                    ok = wait_until_solved(page, echo=echo)
                if not ok:
                    echo(f"Captcha de {url} não foi resolvido: a sessão NÃO foi gravada.")
                    return False
            tmp = output.with_suffix(output.suffix + ".tmp")
            context.storage_state(path=str(tmp))
            tmp.replace(output)
        finally:
            browser.close()
    return True
