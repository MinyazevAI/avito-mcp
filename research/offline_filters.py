"""Офлайн-проверка filters.JS_EXTRACT_FILTERS на фикстурах: .venv/Scripts/python.exe research/offline_filters.py"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import filters  # noqa: E402


async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(channel="msedge", headless=True)
        page = await b.new_page()
        await page.route("**/*", lambda r: r.abort())
        for sc in ("flats", "phones", "services"):
            html = (ROOT / "tests" / "fixtures" / f"{sc}_filters.html").read_text(encoding="utf-8")
            await page.set_content(html, wait_until="domcontentloaded")
            groups = await page.evaluate(filters.JS_EXTRACT_FILTERS, filters.FILTERS_ROOT)
            print(f"== {sc}: {len(groups)}")
            for line in filters.summarize(groups):
                print("  ", line)
        await b.close()

asyncio.run(main())
