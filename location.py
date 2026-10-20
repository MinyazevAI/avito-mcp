"""
Окно «Город или регион» Авито (popup-location/*): выбор города, а также районы, метро и адрес.
Открывается из шапки (search-form/change-location) и из фильтра «Где искать» (geoFilters/select).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import urlparse

from browser import browser
from catalog import norm

log = logging.getLogger("avito-mcp")

POPUP = '[data-marker="popup-location/popup"]'
REGION_INPUT = '[data-marker="popup-location/region/search-input"]'
REGION_OPTION = '[data-marker^="popup-location/region/option"]'
SAVE_BUTTON = '[data-marker="popup-location/save-button"]'


async def center(loc) -> None:
    """Прокрутить элемент в центр: у краёв окна бывают слои, которые перехватывают клики."""
    try:
        await loc.evaluate("e => e.scrollIntoView({block: 'center', inline: 'nearest'})")
        await asyncio.sleep(0.3)
    except Exception:  # noqa: BLE001
        pass


async def open_popup(page, opener: str) -> bool:
    if await page.locator(POPUP).count():
        return True
    btn = page.locator(f'[data-marker="{opener}"]').first
    if not await btn.count():
        return False
    await center(btn)
    await btn.click(timeout=8000)
    try:
        await page.wait_for_selector(POPUP, timeout=8000)
    except Exception:  # noqa: BLE001
        return False
    await asyncio.sleep(0.8)
    return True


async def wait_url_change(page, old_url: str, timeout: float = 20.0) -> bool:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if page.url != old_url:
            return True
        await asyncio.sleep(0.4)
    return False


async def select_city_ui(page, city: str) -> str | None:
    """Выбирает город в окне «Город или регион» и возвращает слаг города из нового URL (или None)."""
    if not await open_popup(page, "search-form/change-location"):
        log.warning("Окно выбора города не открылось")
        return None
    inp = page.locator(f"{POPUP} {REGION_INPUT}").first
    if not await inp.count():
        return None
    await inp.click()
    await inp.press("Control+A")
    await inp.press("Backspace")
    await inp.type(city, delay=70)
    await asyncio.sleep(1.8)
    options = page.locator(f"{POPUP} {REGION_OPTION}")
    target, want = None, norm(city)
    for i in range(await options.count()):
        opt = options.nth(i)
        if not await opt.is_visible():
            continue
        text = norm(await opt.inner_text())
        if target is None:
            target = opt  # первая подсказка — запасной вариант
        if text.startswith(want) or want in text.split(",")[0]:
            target = opt
            break
    if target is None:
        log.warning("Подсказок для города «%s» нет", city)
        await page.keyboard.press("Escape")
        return None
    await target.click()
    await asyncio.sleep(1.0)
    save = page.locator(f"{POPUP} {SAVE_BUTTON}").first
    if not await save.count():
        return None
    old = page.url
    await browser.pace()
    await save.click()
    await wait_url_change(page, old)
    await browser.settle(page)
    parts = [x for x in urlparse(page.url).path.split("/") if x]
    slug = parts[0] if parts else None
    return slug if slug and re.fullmatch(r"[a-z0-9_\-]+", slug) else None


# ---------------------------------------------------------------------------
# Районы, метро, адрес и радиус (avito_set_location)
# ---------------------------------------------------------------------------
TABS = {"district": "tab-marker/tab(districtsTab)", "metro": "tab-marker/tab(metroTab)",
        "address": "tab-marker/tab(addressesTab)"}
ADDRESS_INPUT = '[data-marker="geo/search-input"]'

# Видимые варианты внутри окна (районы, станции, подсказки адреса); помечаем data-mcp-opt
JS_POPUP_OPTIONS = r"""
(sel) => {
  document.querySelectorAll('[data-mcp-opt]').forEach(e => e.removeAttribute('data-mcp-opt'));
  const clean = (s) => (s || '').replace(/[\s\u00a0]+/g, ' ').trim();
  const p = document.querySelector(sel);
  if (!p) return [];
  const out = [], seen = new Set();
  for (const e of p.querySelectorAll('label, [role=checkbox], [role=option], [role=radio], li, button, [data-marker*="option"], [data-marker*="suggest"]')) {
    const r = e.getBoundingClientRect();
    const t = clean(e.innerText);
    if (!t || t.length > 90 || r.width === 0 || r.height === 0) continue;
    if (e.closest('[data-marker^="tab-marker"]') || e.matches('[data-marker="popup-location/save-button"], [data-marker="popup-location/close"]')) continue;
    if (seen.has(t)) continue;
    seen.add(t);
    e.setAttribute('data-mcp-opt', String(out.length));
    const inp = e.querySelector('input');
    out.push({text: t, marker: e.getAttribute('data-marker'), role: e.getAttribute('role'),
              checked: e.getAttribute('aria-checked') === 'true' || !!(inp && inp.checked)});
  }
  return out;
}
"""


async def popup_options(page) -> list[dict]:
    return await page.evaluate(JS_POPUP_OPTIONS, POPUP)


async def click_option(page, i: int) -> None:
    loc = page.locator(f'{POPUP} [data-mcp-opt="{i}"]').first
    await center(loc)
    await loc.click(timeout=8000)
    await asyncio.sleep(0.9)


async def has_tab(page, kind: str) -> bool:
    return await page.locator(f'{POPUP} [data-marker="{TABS[kind]}"]').count() > 0


async def open_tab(page, kind: str) -> bool:
    tab = page.locator(f'{POPUP} [data-marker="{TABS[kind]}"]').first
    if not await tab.count():
        return False
    await tab.click(timeout=8000)
    await asyncio.sleep(1.2)
    return True


def _strip_kind(s: str) -> str:
    """«Кировский район», «р-н Кировский», «м. Кремлёвская» -> «Кировский», «Кремлёвская»."""
    s = re.sub(r"\b(р-н|район|районы|мкр|микрорайон|метро|м\.|ст\.|станция)\b", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" ,.")


async def pick_in_list(page, kind: str, names: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Отмечает районы или станции на открытой вкладке. Возвращает (выбрано, не найдено, примеры вариантов)."""
    from filters import fuzzy_index  # локальный импорт: filters тянет parsing, а он — browser

    chosen, missing = [], []
    opts = await popup_options(page)
    texts = [_strip_kind(o["text"]) for o in opts]
    for name in names:
        i = fuzzy_index(_strip_kind(name), texts)
        if i is None:
            # длинный список: пробуем поиск внутри вкладки, если он есть
            search = page.locator(f"{POPUP} input:visible").filter(has_not=page.locator(REGION_INPUT))
            for k in range(await search.count()):
                inp = search.nth(k)
                if await inp.get_attribute("data-marker") in ("popup-location/region/search-input",):
                    continue
                await inp.fill("")
                await inp.type(_strip_kind(name), delay=60)
                await asyncio.sleep(1.2)
                opts = await popup_options(page)
                texts = [_strip_kind(o["text"]) for o in opts]
                i = fuzzy_index(_strip_kind(name), texts)
                break
        if i is None:
            missing.append(name)
            continue
        if not opts[i]["checked"]:
            await click_option(page, i)
        chosen.append(opts[i]["text"])
        opts = await popup_options(page)
        texts = [_strip_kind(o["text"]) for o in opts]
    return chosen, missing, [o["text"] for o in opts[:30]]


async def set_address(page, address: str, radius_km: float | None) -> dict:
    res: dict = {}
    inp = page.locator(f"{POPUP} {ADDRESS_INPUT}").first
    if not await inp.count():
        return {"error": "Поле адреса не найдено."}
    await inp.click()
    await inp.fill("")
    await inp.type(address, delay=60)
    await asyncio.sleep(2.0)
    opts = await popup_options(page)
    log.info("Подсказки адреса: %s", [o["text"] for o in opts[:8]])
    from filters import fuzzy_index

    texts = [o["text"] for o in opts]
    i = fuzzy_index(address, texts)
    if i is None:
        i = next((k for k, o in enumerate(opts) if o["role"] == "option" or "suggest" in (o["marker"] or "")), None)
    if i is None:
        return {"error": "Авито не предложило такой адрес.", "suggestions": texts[:8]}
    await click_option(page, i)
    res["address"] = texts[i]
    if radius_km is not None:
        opts = await popup_options(page)
        km = [(k, o) for k, o in enumerate(opts) if re.search(r"\d+(?:[.,]\d+)?\s*км|\d+\s*м\b", o["text"])]
        if not km:
            res["radius_error"] = "Выбор радиуса в окне Авито не найден."
        else:
            def to_km(t: str) -> float:
                m = re.search(r"(\d+(?:[.,]\d+)?)\s*(км|м)", t)
                v = float(m.group(1).replace(",", "."))
                return v if m.group(2) == "км" else v / 1000
            best = min(km, key=lambda ko: (to_km(ko[1]["text"]) < radius_km, abs(to_km(ko[1]["text"]) - radius_km)))
            await click_option(page, best[0])
            res["radius"] = best[1]["text"]
    return res


async def save_popup(page) -> bool:
    save = page.locator(f"{POPUP} {SAVE_BUTTON}").first
    if not await save.count():
        return False
    old = page.url
    await browser.pace()
    await center(save)
    await save.click(timeout=8000)
    await wait_url_change(page, old)
    await browser.settle(page)
    return True
