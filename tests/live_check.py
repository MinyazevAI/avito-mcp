#!/usr/bin/env python3
"""
Ручная проверка инструментов на живом сайте (видимый Edge, профиль tests/.browser-profile).
Один сценарий за запуск:
  .venv/Scripts/python.exe tests/live_check.py open_flats
Список сценариев — в SCENARIOS. Если Авито покажет проверку, скрипт не ждёт ввода: опрашивает
страницу раз в 5 секунд до 5 минут и пишет об этом в stderr, затем повторяет вызов.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("AVITO_MCP_PROFILE_DIR", str(ROOT / "tests" / ".browser-profile"))
sys.path.insert(0, str(ROOT))

import parsing  # noqa: E402
import server  # noqa: E402
from browser import browser, is_blocked  # noqa: E402

SCENARIOS = {
    "open_flats": ("avito_open_search", dict(city="Казань", category="квартиры", price_max=12_000_000)),
    "open_phones": ("avito_open_search", dict(city="Казань", category="мобильные телефоны", query="iPhone 13",
                                              price_max=30_000, sort="price_asc")),
    "open_services": ("avito_open_search", dict(city="Казань", query="ремонт стиральных машин", sort="date")),
    "open_landing": ("avito_open_search", dict(city="Казань", category="ремонт техники")),
    "open_city": ("avito_open_search", dict(city="Набережные Челны", category="телефоны", sort="price_desc")),
    "open_unknown": ("avito_open_search", dict(city="Казань", category="зимние шины R17")),
}


def err(*a) -> None:
    print(*a, file=sys.stderr, flush=True)


async def wait_unblocked() -> bool:
    page = await browser.page()
    err("!!! Авито показало проверку. Пройдите её в окне Edge — жду до 5 минут (опрос раз в 5 с).")
    started = time.monotonic()
    while time.monotonic() - started < 300:
        await asyncio.sleep(5)
        if not await is_blocked(page):
            err("Проверка пройдена, повторяю вызов.")
            return True
    err("Проверка не пройдена за 5 минут.")
    return False


async def call(tool: str, **kwargs):
    """Вызов инструмента; при проверке Авито — ждём, пока её пройдут, и повторяем."""
    fn = getattr(server, tool)
    started = time.monotonic()
    res = await fn(**kwargs)
    if res.get("status") == "blocked" and await wait_unblocked():
        started = time.monotonic()
        res = await fn(**kwargs)
    res["_elapsed_s"] = round(time.monotonic() - started, 1)
    short = {k: v for k, v in res.items() if k not in ("filters", "items", "subcategories")}
    err(f"--- {tool}({json.dumps(kwargs, ensure_ascii=False)[:300]})")
    err(json.dumps(short, ensure_ascii=False, indent=1)[:4000])
    return res


async def flow_flats():
    r = await call("avito_open_search", city="Казань", category="квартиры", price_max=12_000_000)
    r = await call("avito_apply_filters", url=r["url"], filters=[
        {"name": "Количество комнат", "values": ["Студия", "1"]},
        {"name": "Площадь", "from": 30},
        {"name": "Год постройки", "from": 2006},
    ])
    return await call("avito_set_location", url=r["url"], district="Кировский")


async def flow_phones():
    r = await call("avito_open_search", city="Казань", query="iPhone 13", category="мобильные телефоны",
                   price_max=30_000)
    return await call("avito_apply_filters", url=r["url"], filters=[
        {"name": "Состояние", "values": ["б/у"]},
        {"name": "Авито Доставка", "value": True},
        {"name": "Модель", "values": ["iPhone 13"]},
    ])


async def flow_services():
    r = await call("avito_open_search", city="Казань", query="ремонт стиральных машин")
    g = await call("avito_get_filters", url=r["url"])
    err("FILTERS:", json.dumps(g.get("filters"), ensure_ascii=False)[:2500])
    return await call("avito_apply_filters", url=r["url"], filters=[
        {"name": "Производители", "values": ["Bosch"]},
        {"name": "Кто оказывает услуги", "values": ["Частный исполнитель"]},
    ])


async def flow_brand():
    r = await call("avito_open_search", city="Казань", category="мобильные телефоны", price_max=30_000)
    return await call("avito_apply_filters", url=r["url"], filters=[
        {"name": "Производитель", "values": ["Samsung"]},
    ])


FLOWS = {"flow_flats": flow_flats, "flow_phones": flow_phones, "flow_services": flow_services,
         "flow_brand": flow_brand}


async def main(name: str) -> int:
    if name in FLOWS:
        res = await FLOWS[name]()
        out = ROOT / "tests" / ".live"
        out.mkdir(exist_ok=True)
        (out / f"{name}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(res, ensure_ascii=False, indent=1))
        await browser.close()
        return 0 if res.get("status") in ("ok", "partial") else 1
    tool, kwargs = SCENARIOS[name]
    fn = getattr(server, tool)
    started = time.monotonic()
    res = await fn(**kwargs)
    if res.get("status") == "blocked" and await wait_unblocked():
        started = time.monotonic()
        res = await fn(**kwargs)
    elapsed = time.monotonic() - started
    # Цены первых объявлений на открытой странице — для проверки сортировки (без новой загрузки)
    try:
        page = await browser.page()
        raw = await page.evaluate(parsing.JS_SEARCH)
        res["_first_prices"] = [parsing.price(r.get("price")) for r in raw[:8]]
        res["_first_dates"] = [parsing.clean(r.get("date")) for r in raw[:5]]
    except Exception as e:  # noqa: BLE001
        res["_first_prices"] = f"n/a: {e}"
    res["_elapsed_s"] = round(elapsed, 1)
    out = ROOT / "tests" / ".live"
    out.mkdir(exist_ok=True)
    (out / f"{name}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=1))
    await browser.close()
    return 0 if res.get("status") in ("ok", "landing") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1])))
