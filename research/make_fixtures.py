#!/usr/bin/env python3
"""
Делает фикстуры tests/fixtures/*.html из полных дампов research/dumps/<сценарий>/*_full.html — офлайн,
без загрузок с Авито. Обрезка (JS_TRIM из explore.py): без скриптов/стилей, без данных продавцов,
в выдаче — первые 6 объявлений. После записи проверяет, что в фикстурах нет телефонных номеров.
  .venv/Scripts/python.exe research/make_fixtures.py
"""
import asyncio
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from explore import DUMPS, FIXTURES, JS_TRIM  # noqa: E402

PHONE_RE = re.compile(r"(?:\+7|\b8)[\s\-(]*9\d{2}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")

JOBS = [  # (дамп, селектор корня, макс. объявлений, имя фикстуры)
    ("serp_full.html", None, 6, "{sc}_serp.html"),
    ("serp_full.html", '[data-marker="search-filters"]', 0, "{sc}_filters.html"),
    ("card_full.html", None, 0, "{sc}_card.html"),
    ("landing_full.html", None, 6, "{sc}_landing.html"),
]


async def main():
    FIXTURES.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        b = await pw.chromium.launch(channel="msedge", headless=True)
        page = await b.new_page()
        await page.route("**/*", lambda r: r.abort())
        for sc_dir in sorted(p for p in DUMPS.iterdir() if p.is_dir()):
            sc = sc_dir.name
            for dump, root, max_items, name in JOBS:
                src = sc_dir / dump
                if not src.exists():
                    continue
                await page.set_content(src.read_text(encoding="utf-8"), wait_until="domcontentloaded")
                html = await page.evaluate(JS_TRIM, [root, max_items, sc == "services"])
                if not html:
                    print(f"{sc}: {dump} — корень {root} не найден")
                    continue
                html = "<!doctype html>\n" + html
                phones = PHONE_RE.findall(html)
                out = FIXTURES / name.format(sc=sc)
                out.write_text(html, encoding="utf-8")
                print(f"{out.relative_to(FIXTURES.parent.parent)}: {len(html) // 1024} КБ, телефонов найдено: {len(phones)}")
        await b.close()


if __name__ == "__main__":
    asyncio.run(main())
