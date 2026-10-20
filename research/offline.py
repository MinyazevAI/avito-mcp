#!/usr/bin/env python3
"""
Офлайн-прогон JS-разборщика на сохранённом HTML (без загрузок с Авито).
  .venv/Scripts/python.exe research/offline.py research/extract_filters.js tests/fixtures/flats_filters.html [out.json]
"""
import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright


async def main(js_path: str, html_path: str, out: str | None):
    js = Path(js_path).read_text(encoding="utf-8")
    html = Path(html_path).read_text(encoding="utf-8")
    async with async_playwright() as pw:
        b = await pw.chromium.launch(channel="msedge", headless=True)
        page = await b.new_page()
        await page.route("**/*", lambda r: r.abort())  # никаких сетевых запросов
        await page.set_content(html, wait_until="domcontentloaded")
        res = await page.evaluate(js, None)
        await b.close()
    text = json.dumps(res, ensure_ascii=False, indent=1)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
