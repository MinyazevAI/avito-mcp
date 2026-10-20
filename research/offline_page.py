"""Офлайн-проверка parsing.JS_PAGE_INFO / JS_SEARCH / JS_LISTING на фикстурах."""
import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import parsing  # noqa: E402


async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(channel="msedge", headless=True)
        page = await b.new_page()
        await page.route("**/*", lambda r: r.abort())
        for name in ("flats_serp", "phones_serp", "services_serp", "services_landing"):
            await page.set_content((ROOT / "tests/fixtures" / f"{name}.html").read_text(encoding="utf-8"))
            info = await page.evaluate(parsing.JS_PAGE_INFO)
            items = [parsing.search_item(r) for r in await page.evaluate(parsing.JS_SEARCH)]
            print(name, json.dumps(parsing.page_summary(info), ensure_ascii=False), "| tiles:", len(info["tiles"]),
                  "| items:", len(items), json.dumps(items[:1], ensure_ascii=False)[:300])
        for name in ("flats_card", "phones_card", "services_card"):
            await page.set_content((ROOT / "tests/fixtures" / f"{name}.html").read_text(encoding="utf-8"))
            d = parsing.listing_details(await page.evaluate(parsing.JS_LISTING), "x")
            d.pop("description", None)
            print(name, json.dumps(d, ensure_ascii=False)[:700])
        await b.close()

asyncio.run(main())
