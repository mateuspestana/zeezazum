"""Rode este script LOCALMENTE (não no servidor/EC2) para gerar o storage_state.json
usado pelo scraper. Abre um browser visível, você loga manualmente no Instagram,
aperta Enter no terminal, e a sessão (cookies) é salva em disco.

Depois, copie o arquivo gerado para o servidor (scp/rsync) antes de rodar
`zeezazum scrape` lá — o servidor roda headless e não faz login sozinho.

Uso:
    uv run python scripts/local_login.py [--output sessions/instagram_storage_state.json] [--browser chromium|firefox]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="sessions/instagram_storage_state.json")
    parser.add_argument("--browser", default="chromium", choices=["chromium", "firefox"])
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser_type = getattr(pw, args.browser)
        browser = browser_type.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://www.instagram.com/accounts/login/")

        print("\nFaça login manualmente na janela do navegador que abriu.")
        input("Depois de logar (e fechar popups de 'salvar login'/notificações), pressione Enter aqui... ")

        context.storage_state(path=str(output_path))
        browser.close()

    print(f"\nSessão salva em: {output_path}")
    print("Copie esse arquivo para o servidor (ex.: scp) antes de rodar `zeezazum scrape` lá.")


if __name__ == "__main__":
    main()
