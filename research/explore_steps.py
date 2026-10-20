"""
Фазы apply и city для research/explore.py: выставление фильтров через интерфейс, кнопка применения,
попап «Где искать» (районы / метро / радиус), смена города.
Кликаем только в панели фильтров, строке поиска и выборе города/района.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from explore import DUMPS, JS_CONTROLS, JS_MARKERS, JS_PAGE_INFO, Session, err, out, save_json, shot  # noqa: F401

HERE = Path(__file__).resolve().parent
EXTRACT_JS = (HERE / "extract_filters.js").read_text(encoding="utf-8")
FILTERS = '[data-marker="search-filters"]'

JS_BUTTONS = r"""
(rootSel) => {
  const root = rootSel ? document.querySelector(rootSel) : document;
  if (!root) return [];
  return Array.from(root.querySelectorAll('button, a, [role=button]'))
    .filter(b => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
    .map(b => ({text: (b.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 60),
                marker: b.getAttribute('data-marker'), tag: b.tagName.toLowerCase(),
                href: b.tagName === 'A' ? (b.getAttribute('href') || '').slice(0, 200) : null}))
    .filter(b => b.text);
}
"""

JS_MARK_NEW_LAYER = r"""
(before) => {
  // помечаем появившийся слой (диалог/попап): role=dialog, aria-modal или новый data-marker верхнего уровня
  document.querySelectorAll('[data-explore-layer]').forEach(e => e.removeAttribute('data-explore-layer'));
  const cands = Array.from(document.querySelectorAll('[role=dialog], [aria-modal=true], [data-marker*="popup" i], [data-marker*="modal" i], [data-marker*="dropdown" i], [role=listbox]'))
    .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
  if (cands.length) { const e = cands[cands.length - 1]; e.setAttribute('data-explore-layer', '1');
                      return {how: 'role', marker: e.getAttribute('data-marker'), role: e.getAttribute('role')}; }
  const set = new Set(before);
  const fresh = Array.from(document.querySelectorAll('[data-marker]')).filter(e => !set.has(e.getAttribute('data-marker')));
  if (!fresh.length) return null;
  let top = fresh[0];
  while (top.parentElement && top.parentElement.closest('[data-marker]') && fresh.includes(top.parentElement.closest('[data-marker]')))
    top = top.parentElement.closest('[data-marker]');
  top.setAttribute('data-explore-layer', '1');
  return {how: 'new-markers', marker: top.getAttribute('data-marker'), count: fresh.length};
}
"""


async def center(loc) -> None:
    """Прокрутить элемент в центр экрана: у нижнего края висит слой portals-container, он перехватывает клики."""
    await loc.evaluate("e => e.scrollIntoView({block: 'center', inline: 'nearest'})")
    await asyncio.sleep(0.4)


async def log_overlays(s: Session) -> None:
    info = await s.page.evaluate("""() => Array.from(document.querySelectorAll('[data-marker="portals-container"] > *, [role=region]'))
      .map(e => { const r = e.getBoundingClientRect();
                  return {role: e.getAttribute('role'), marker: e.getAttribute('data-marker'),
                          text: (e.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 120),
                          rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]}; })
      .filter(x => x.rect[2] > 0)""")
    out("  OVERLAYS:", json.dumps(info, ensure_ascii=False))


async def markers_list(s: Session) -> list[str]:
    return await s.page.evaluate("() => Array.from(new Set(Array.from(document.querySelectorAll('[data-marker]')).map(e => e.getAttribute('data-marker'))))")


async def filters_state(s: Session, name: str, sc: str):
    st = await s.page.evaluate(EXTRACT_JS, FILTERS)
    save_json(DUMPS / sc / f"{name}.json", st)
    for g in st or []:
        if g.get("selected") and g["selected"] != ["Все"] and g["selected"] != ["Неважно"] or g.get("from") or g.get("to") or g.get("text"):
            out(f"  STATE {g.get('name')} [{g['key']}]: sel={g.get('selected')} from={g.get('from')} to={g.get('to')} text={g.get('text')}")
    return st


async def filter_buttons(s: Session, label: str):
    btns = await s.page.evaluate(JS_BUTTONS, FILTERS)
    interesting = [b for b in btns if re.search(r"показать|применить|сбросить|найти|готово", b["text"], re.I)]
    out(f"  BUTTONS[{label}]:", json.dumps(interesting, ensure_ascii=False))
    return interesting


async def click_marker(s: Session, marker: str, what: str = "") -> bool:
    loc = s.page.locator(f'[data-marker="{marker}"]').first
    if not await loc.count():
        out(f"  NO ELEMENT {marker} {what}")
        return False
    await center(loc)
    await loc.click(timeout=8000)
    await asyncio.sleep(1.2)
    out(f"  CLICK {marker} {what} -> url={s.page.url[:150]}")
    return True


async def fill_marker(s: Session, marker: str, value: str):
    loc = s.page.locator(f'[data-marker="{marker}"]').first
    if not await loc.count():
        out(f"  NO INPUT {marker}")
        return False
    await center(loc)
    await loc.click(timeout=8000)
    await loc.fill("")
    await loc.type(value, delay=60)
    await loc.press("Tab")
    await asyncio.sleep(1.5)
    out(f"  FILL {marker}={value} -> value={await loc.input_value()} url={s.page.url[:150]}")
    return True


async def open_layer(s: Session, marker: str, sc: str, name: str):
    before = await markers_list(s)
    if not await click_marker(s, marker, f"(open {name})"):
        return None
    await asyncio.sleep(1.5)
    layer = await s.page.evaluate(JS_MARK_NEW_LAYER, before)
    out(f"  LAYER {name}:", json.dumps(layer, ensure_ascii=False))
    await shot(s, f"{sc}_{name}")
    after = await markers_list(s)
    save_json(DUMPS / sc / f"{name}_new_markers.json", [m for m in after if m not in set(before)])
    if layer:
        save_json(DUMPS / sc / f"{name}_markers.json", await s.page.evaluate(JS_MARKERS, "[data-explore-layer]"))
        save_json(DUMPS / sc / f"{name}_controls.json", await s.page.evaluate(JS_CONTROLS, "[data-explore-layer]"))
        save_json(DUMPS / sc / f"{name}_buttons.json", await s.page.evaluate(JS_BUTTONS, "[data-explore-layer]"))
        html = await s.page.evaluate("() => { const e = document.querySelector('[data-explore-layer]'); return e ? e.outerHTML : null; }")
        (DUMPS / sc / f"{name}.html").write_text(html or "", encoding="utf-8")
    return layer


async def click_text_in(s: Session, scope: str, pattern: str, what: str = "") -> str | None:
    loc = s.page.locator(f"{scope} :is(label, button, a, li, [role=option], [role=checkbox], [role=tab], [role=button], span)")
    loc = loc.filter(has_text=re.compile(pattern, re.I))
    n = await loc.count()
    for i in range(n - 1, -1, -1):  # самый вложенный — последний
        el = loc.nth(i)
        if await el.is_visible():
            t = (await el.inner_text()).strip()[:60]
            await center(el)
            await el.click(timeout=8000)
            await asyncio.sleep(1.5)
            out(f"  CLICK TEXT '{t}' {what} -> url={s.page.url[:150]}")
            return t
    out(f"  NO TEXT /{pattern}/ in {scope} {what}")
    return None


async def submit(s: Session, scope: str, pattern: str = r"^\s*Показать") -> bool:
    """Нажимает кнопку применения и ждёт новую выдачу (считается загрузкой страницы)."""
    loc = s.page.locator(f"{scope} :is(button, a, [role=button])").filter(has_text=re.compile(pattern, re.I))
    n = await loc.count()
    target = None
    for i in range(n):
        if await loc.nth(i).is_visible():
            target = loc.nth(i)
            break
    if target is None:
        out(f"  NO SUBMIT /{pattern}/ in {scope}")
        return False
    text = (await target.inner_text()).strip()
    marker = await target.get_attribute("data-marker")
    old_url = s.page.url
    await s.pace()
    s.mark_load(f"submit '{text}' [{marker}]")
    await center(target)
    await target.click(timeout=8000)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 20 and s.page.url == old_url:
        await asyncio.sleep(0.5)
    await s.settle()
    await s.wait_if_blocked()
    await s.wait_content()
    out(f"  SUBMITTED '{text}' [{marker}] in {time.monotonic() - t0:.1f}s url_changed={s.page.url != old_url}")
    return True


async def report(s: Session, sc: str, label: str):
    info = await s.page.evaluate(JS_PAGE_INFO)
    out(f"PAGE[{label}]:", json.dumps({k: info[k] for k in ('url', 'h1', 'count', 'items')}, ensure_ascii=False))
    save_json(DUMPS / sc / f"page_{label}.json", info)
    return info


# ---------------------------------------------------------------------------
async def apply_flats(s: Session):
    sc = "flats"
    await s.goto("https://www.avito.ru/kazan/kvartiry/prodam-ASgBAgICAUSSA8YQ")
    await report(s, sc, "start")
    await log_overlays(s)
    await shot(s, f"{sc}_start_bottom")
    await filter_buttons(s, "initial")
    await click_marker(s, "params[549]/checkbox/5695", "Студия")
    await filter_buttons(s, "after-checkbox")
    await shot(s, f"{sc}_after_checkbox")
    await click_marker(s, "params[549]/checkbox/5696", "1 комната")
    await fill_marker(s, "price-to/input", "12000000")
    await fill_marker(s, "params[578]-from/input", "30")
    await fill_marker(s, "params[164901]-from/input", "2006")
    await filter_buttons(s, "after-fill")
    await shot(s, f"{sc}_after_fill")
    await filters_state(s, "state_before_submit", sc)

    # Попап «Где искать»: районы / метро / радиус
    layer = await open_layer(s, "geoFilters/select", sc, "geo_popup")
    if layer:
        btns = await s.page.evaluate(JS_BUTTONS, "[data-explore-layer]")
        out("  GEO BUTTONS:", json.dumps(btns[:40], ensure_ascii=False))
        tab = await click_text_in(s, "[data-explore-layer]", r"^\s*Район", "(вкладка районов)")
        if tab:
            await shot(s, f"{sc}_geo_districts")
            save_json(DUMPS / sc / "geo_districts_controls.json", await s.page.evaluate(JS_CONTROLS, "[data-explore-layer]"))
            (DUMPS / sc / "geo_districts.html").write_text(
                await s.page.evaluate("() => document.querySelector('[data-explore-layer]').outerHTML"), encoding="utf-8")
        picked = await click_text_in(s, "[data-explore-layer]", r"^\s*Кировский", "(район)")
        await shot(s, f"{sc}_geo_picked")
        btns = await s.page.evaluate(JS_BUTTONS, "[data-explore-layer]")
        out("  GEO BUTTONS AFTER PICK:", json.dumps([b for b in btns if not b.get('href')][:30], ensure_ascii=False))
        if picked:
            ok = await submit(s, "[data-explore-layer]", r"Показать|Применить|Готово|Выбрать")
            if not ok:
                await s.page.keyboard.press("Escape")
        else:
            await s.page.keyboard.press("Escape")
        await asyncio.sleep(1.5)
    await report(s, sc, "after_geo")
    await filter_buttons(s, "after-geo")
    await filters_state(s, "state_after_geo", sc)
    if await submit(s, FILTERS):
        await report(s, sc, "after_submit")
    await shot(s, f"{sc}_applied")
    await filters_state(s, "state_after_submit", sc)
    (DUMPS / sc / "applied_full.html").write_text(await s.page.content(), encoding="utf-8")


async def apply_phones(s: Session):
    sc = "phones"
    await s.goto("https://www.avito.ru/kazan/telefony/mobile-ASgBAgICAUSwwQ2I_Dc")
    await report(s, sc, "start")
    layer = await open_layer(s, "params[110618]/multiselect-outline-input", sc, "brand_popup")
    if layer:
        inp = s.page.locator('[data-marker="params[110618]/multiselect-outline-input/input"]').first
        if await inp.count():
            await inp.type("Apple", delay=80)
            await asyncio.sleep(1.5)
            await shot(s, f"{sc}_brand_typed")
            save_json(DUMPS / sc / "brand_typed_controls.json", await s.page.evaluate(JS_CONTROLS, "[data-explore-layer]"))
        await click_text_in(s, "[data-explore-layer]", r"^\s*Apple\s*$", "(бренд)")
        await s.page.keyboard.press("Escape")
        await asyncio.sleep(1.0)
    await filter_buttons(s, "after-brand")
    await filters_state(s, "state_after_brand", sc)
    # Модель могла появиться после выбора бренда
    after = await s.page.evaluate(EXTRACT_JS, FILTERS)
    out("  GROUPS AFTER BRAND:", [g.get("name") for g in after][:12])
    for lab in ("Отличное", "Хорошее", "Удовлетворительное"):
        await click_text_in(s, FILTERS, rf"^\s*{lab}\s*$", "(состояние)")
    await click_marker(s, "d/checkbox/1", "С Авито Доставкой")
    await fill_marker(s, "price-to/input", "30000")
    await filter_buttons(s, "after-fill")
    await shot(s, f"{sc}_after_fill")
    await filters_state(s, "state_before_submit", sc)
    if await submit(s, FILTERS):
        await report(s, sc, "after_submit")
    await shot(s, f"{sc}_applied")
    await filters_state(s, "state_after_submit", sc)
    (DUMPS / sc / "applied_full.html").write_text(await s.page.content(), encoding="utf-8")
    # Меню сортировки: только открыть и прочитать варианты
    layer = await open_layer(s, "sort/title", sc, "sort_popup")
    btns = await s.page.evaluate(JS_BUTTONS, None)
    out("  SORT OPTIONS:", json.dumps([b for b in btns if b.get("href") and "s=" in (b["href"] or "")], ensure_ascii=False))
    await s.page.keyboard.press("Escape")


async def apply_services(s: Session):
    sc = "services"
    await s.goto("https://www.avito.ru/kazan?q=%D1%80%D0%B5%D0%BC%D0%BE%D0%BD%D1%82+%D1%81%D1%82%D0%B8%D1%80%D0%B0%D0%BB%D1%8C%D0%BD%D1%8B%D1%85+%D0%BC%D0%B0%D1%88%D0%B8%D0%BD")
    await report(s, sc, "start")
    await filter_buttons(s, "initial")
    await click_text_in(s, FILTERS, r"^\s*Bosch\s*$", "(производитель)")
    await click_text_in(s, FILTERS, r"^\s*Сегодня\s*$", "(когда)")
    await filter_buttons(s, "after-fill")
    await shot(s, f"{sc}_after_fill")
    await filters_state(s, "state_before_submit", sc)
    if await submit(s, FILTERS):
        await report(s, sc, "after_submit")
    await shot(s, f"{sc}_applied")
    await filters_state(s, "state_after_submit", sc)


async def phase_apply(s: Session, sc: str):
    await {"flats": apply_flats, "phones": apply_phones, "services": apply_services}[sc](s)


async def phase_city(s: Session, city: str = "Набережные Челны"):
    sc = "city"
    await s.goto("https://www.avito.ru/kazan/telefony/mobile-ASgBAgICAUSwwQ2I_Dc?q=iphone")
    await report(s, sc, "start")
    layer = await open_layer(s, "search-form/change-location", sc, "location_popup")
    if not layer:
        return
    inputs = s.page.locator("[data-explore-layer] input:visible")
    out("  INPUTS:", await inputs.count())
    if await inputs.count():
        inp = inputs.first
        await inp.click()
        await inp.fill("")
        await inp.type(city, delay=80)
        await asyncio.sleep(2.5)
        await shot(s, f"{sc}_typed")
        save_json(DUMPS / sc / "typed_controls.json", await s.page.evaluate(JS_CONTROLS, "[data-explore-layer]"))
        save_json(DUMPS / sc / "typed_markers.json", await s.page.evaluate(JS_MARKERS, None))
        (DUMPS / sc / "typed.html").write_text(
            await s.page.evaluate("() => document.querySelector('[data-explore-layer]').outerHTML"), encoding="utf-8")
        await click_text_in(s, "[data-explore-layer]", rf"^\s*{city}", "(город)")
        await shot(s, f"{sc}_picked")
        btns = await s.page.evaluate(JS_BUTTONS, "[data-explore-layer]")
        out("  LOC BUTTONS:", json.dumps(btns[:20], ensure_ascii=False))
        if await submit(s, "[data-explore-layer]", r"Показать|Применить|Готово|Выбрать|Сохранить"):
            await report(s, sc, "after_city")


# ---------------------------------------------------------------------------
POPUP = '[data-marker="popup-location/popup"]'
JS_POPUP_CONTENT = r"""
() => {
  const p = document.querySelector('[data-marker="popup-location/popup"]');
  if (!p) return null;
  const clean = (s) => (s || '').replace(/[\s\u00a0]+/g, ' ').trim();
  const labels = Array.from(p.querySelectorAll('label, [role=checkbox], [role=option], li'))
    .filter(e => e.getBoundingClientRect().width > 0)
    .map(e => ({tag: e.tagName.toLowerCase(), marker: e.getAttribute('data-marker'), role: e.getAttribute('role'),
                checked: e.getAttribute('aria-checked') || ((e.querySelector('input') || {}).checked ? 'true' : null),
                text: clean(e.innerText).slice(0, 60)}))
    .filter(x => x.text);
  const tabs = Array.from(p.querySelectorAll('[data-marker^="tab-marker/tab"]'))
    .map(e => ({marker: e.getAttribute('data-marker'), text: clean(e.innerText),
                selected: e.getAttribute('aria-selected')}));
  const inputs = Array.from(p.querySelectorAll('input')).map(e => ({marker: e.getAttribute('data-marker'),
                placeholder: e.getAttribute('placeholder'), value: e.value, type: e.type}));
  const save = p.querySelector('[data-marker="popup-location/save-button"]');
  return {tabs, inputs, labels_count: labels.length, labels: labels.slice(0, 80), save: save ? clean(save.innerText) : null};
}
"""


async def popup_dump(s: Session, sc: str, name: str):
    c = await s.page.evaluate(JS_POPUP_CONTENT)
    save_json(DUMPS / sc / f"{name}.json", c)
    await shot(s, f"{sc}_{name}")
    if c:
        out(f"  POPUP[{name}] tabs={c['tabs']} inputs={c['inputs']} save={c['save']!r} labels={c['labels_count']}")
        out("    first labels:", json.dumps([l['text'] + ('*' if l['checked'] == 'true' else '') for l in c['labels'][:40]], ensure_ascii=False))
    else:
        out(f"  POPUP[{name}] нет")
    return c


async def phase_geo(s: Session, start_url: str):
    sc = "flats"
    await s.goto(start_url)
    await report(s, sc, "geo_start")
    await filters_state(s, "state_geo_start", sc)
    await click_marker(s, "geoFilters/select", "(Где искать)")
    await s.page.wait_for_selector(POPUP, timeout=8000)
    (DUMPS / sc / "geo_popup.html").write_text(await s.page.evaluate(f"() => document.querySelector('{POPUP}').outerHTML"), encoding="utf-8")
    await popup_dump(s, sc, "geo_addresses")
    # Адреса: ввести адрес и посмотреть, появится ли радиус
    addr = s.page.locator(f'{POPUP} [data-marker="geo/search-input"]').first
    if await addr.count():
        await addr.click()
        await addr.type("Чистопольская 20", delay=80)
        await asyncio.sleep(2.5)
        await popup_dump(s, sc, "geo_address_typed")
        save_json(DUMPS / sc / "geo_address_markers.json", await s.page.evaluate(JS_MARKERS, POPUP))
        await addr.fill("")
        await asyncio.sleep(1.0)
    await click_marker(s, "tab-marker/tab(metroTab)", "(Метро)")
    await popup_dump(s, sc, "geo_metro")
    save_json(DUMPS / sc / "geo_metro_markers.json", await s.page.evaluate(JS_MARKERS, POPUP))
    await click_marker(s, "tab-marker/tab(districtsTab)", "(Районы)")
    c = await popup_dump(s, sc, "geo_districts")
    save_json(DUMPS / sc / "geo_districts_markers.json", await s.page.evaluate(JS_MARKERS, POPUP))
    (DUMPS / sc / "geo_districts.html").write_text(await s.page.evaluate(f"() => document.querySelector('{POPUP}').outerHTML"), encoding="utf-8")
    await click_text_in(s, POPUP, r"^\s*Кировский", "(район)")
    await popup_dump(s, sc, "geo_district_picked")
    await submit(s, POPUP, r"Показать")
    await report(s, sc, "geo_applied")
    await filters_state(s, "state_geo_applied", sc)
    await shot(s, f"{sc}_geo_applied")
    (DUMPS / sc / "geo_applied_full.html").write_text(await s.page.content(), encoding="utf-8")


# ---------------------------------------------------------------------------
async def phase_multiselect(s: Session, url: str, marker: str = "params[110618]/multiselect-outline-input", text: str = "Sam"):
    """Разметка выпадающего списка мультиселекта (Производитель/Модель): открыть, ввести текст, снять дамп."""
    sc = "multiselect"
    await s.goto(url)
    await report(s, sc, "start")
    before = await markers_list(s)
    html_before = await s.page.evaluate("() => document.body.innerHTML.length")
    loc = s.page.locator(f'[data-marker="{marker}"]').first
    await center(loc)
    await loc.click(timeout=8000)
    await asyncio.sleep(1.5)
    await shot(s, f"{sc}_opened")
    after = await markers_list(s)
    save_json(DUMPS / sc / "opened_new_markers.json", [m for m in after if m not in set(before)])
    out("  NEW MARKERS (opened):", [m for m in after if m not in set(before)][:60])
    focused = await s.page.evaluate("() => { const e = document.activeElement; return e ? {tag: e.tagName, marker: e.getAttribute('data-marker'), ph: e.getAttribute('placeholder')} : null; }")
    out("  FOCUSED:", focused)
    await s.page.keyboard.type(text, delay=80)
    await asyncio.sleep(2.0)
    await shot(s, f"{sc}_typed")
    after2 = await markers_list(s)
    save_json(DUMPS / sc / "typed_new_markers.json", [m for m in after2 if m not in set(before)])
    out("  NEW MARKERS (typed):", [m for m in after2 if m not in set(before)][:80])
    info = await s.page.evaluate(r"""(text) => {
      const res = [];
      for (const e of document.querySelectorAll('body *')) {
        if (e.children.length > 2) continue;
        const t = (e.innerText || '').trim();
        if (!t || t.length > 60 || !t.toLowerCase().includes(text.toLowerCase())) continue;
        const r = e.getBoundingClientRect(); if (!r.width) continue;
        const chain = []; let p = e; while (p && chain.length < 8) { chain.push(p.tagName.toLowerCase() + (p.getAttribute('role') ? '[role=' + p.getAttribute('role') + ']' : '') + (p.getAttribute('data-marker') ? '{' + p.getAttribute('data-marker') + '}' : '') + (p.getAttribute('aria-checked') ? '(checked=' + p.getAttribute('aria-checked') + ')' : '')); p = p.parentElement; }
        res.push({t, chain: chain.join(' < ')});
      }
      return res.slice(0, 25);
    }""", text)
    for x in info:
        out("   OPT:", x["t"], "::", x["chain"][:400])
    (DUMPS / sc / "typed_body.html").write_text(await s.page.content(), encoding="utf-8")
    await s.page.keyboard.press("Escape")
