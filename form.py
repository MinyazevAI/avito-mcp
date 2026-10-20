"""
Действия в панели фильтров Авито: выставить значения и дождаться новой выдачи.

На Авито фильтры применяются сразу после изменения (кнопки «Показать N объявлений» нет): после клика
по флажку или ввода в поле с потерей фокуса сайт сам меняет URL (?f=…) и перерисовывает выдачу.
Поэтому меняем поля по одному, после каждого ждём смены URL или счётчика, а в конце перечитываем панель.
Кликаем только внутри панели фильтров и её выпадающих списков.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import filters as F
from browser import TIME_BUDGET, browser
from location import center

log = logging.getLogger("avito-mcp")
DEBUG_DIR = os.environ.get("AVITO_MCP_DEBUG_DIR")  # для отладки: скриншоты и разметка шагов мультиселекта


async def debug_dump(page, name: str, extra: Any = None) -> None:
    if not DEBUG_DIR:
        return
    d = Path(DEBUG_DIR)
    d.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(d / f"{name}.png"))
        if extra is not None:
            (d / f"{name}.json").write_text(json.dumps(extra, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("debug_dump %s: %s", name, e)


JS_CHAIN = r"""(sel) => { const out = []; let p = document.querySelector(sel);
  while (p && out.length < 10) { out.push(p.tagName.toLowerCase() + (p.getAttribute('role') ? '[role=' + p.getAttribute('role') + ']' : '')
    + (p.getAttribute('data-marker') ? '{' + p.getAttribute('data-marker') + '}' : '')
    + (p.getAttribute('aria-checked') ? '(checked=' + p.getAttribute('aria-checked') + ')' : '')
    + (p.getAttribute('aria-selected') ? '(selected=' + p.getAttribute('aria-selected') + ')' : '')); p = p.parentElement; }
  return out; }"""

FORM_PAUSE = 0.6  # короткая пауза между действиями в форме
UPDATE_TIMEOUT = 10.0  # сколько ждать обновления выдачи после изменения
ACTION_COST = 5.0  # примерная длительность одного изменения, для бюджета времени

JS_SNAPSHOT = """() => ({url: location.href,
  count: ((document.querySelector('[data-marker="page-title/count"]') || {}).innerText || '').trim()})"""

# Помечает элемент для клика: по data-marker или (для флажков без разметки) по тексту внутри группы
JS_MARK = r"""
([marker, title, label]) => {
  document.querySelectorAll('[data-mcp-target]').forEach(e => e.removeAttribute('data-mcp-target'));
  const clean = (s) => (s || '').replace(/[\s ]+/g, ' ').trim();
  let el = marker ? document.querySelector(`[data-marker="${marker}"]`) : null;
  if (!el && label) {
    const root = document.querySelector('[data-marker="search-filters"]') || document;
    const cands = Array.from(root.querySelectorAll('[role=checkbox], label')).filter(e => clean(e.innerText) === label);
    el = cands.find(e => {
      let p = e.parentElement;
      while (p && p !== root) { const hs = p.querySelectorAll('h5'); if (hs.length === 1) return clean(hs[0].innerText) === title;
                                if (hs.length > 1) return false; p = p.parentElement; }
      return false;
    }) || cands[0];
  }
  if (!el) return false;
  el.setAttribute('data-mcp-target', '1');
  return true;
}
"""

# Мультиселект: разметка выпадающего списка заранее неизвестна, поэтому перед открытием запоминаем
# все элементы страницы, а после ввода текста ищем варианты среди появившихся видимых элементов.
JS_REMEMBER_DOM = "() => { window.__mcpOld = new WeakSet(document.querySelectorAll('body *')); return true; }"

JS_DROPDOWN_OPTIONS = r"""
(marker) => {
  document.querySelectorAll('[data-mcp-opt]').forEach(e => e.removeAttribute('data-mcp-opt'));
  const clean = (s) => (s || '').replace(/[\s\u00a0]+/g, ' ').trim();
  const old = window.__mcpOld || new WeakSet();
  // секция группы: ближайший предок поля, в котором ровно один заголовок <h5>
  const opener = document.querySelector(`[data-marker="${marker}"]`);
  let section = null;
  for (let p = opener && opener.parentElement; p; p = p.parentElement) {
    const n = p.querySelectorAll('h5').length;
    if (n === 1) { section = p; } else if (n > 1) break;
  }
  const pool = new Set();
  if (section) section.querySelectorAll('[role=checkbox], [role=option], label, li').forEach(e => pool.add(e));
  document.querySelectorAll('[role=listbox] [role=option], [role=listbox] [role=checkbox], [role=dialog] [role=checkbox]')
    .forEach(e => pool.add(e));
  for (const e of document.querySelectorAll('body *')) if (!old.has(e) && e.children.length <= 3) pool.add(e);
  const out = [], seenText = new Set();
  for (const e of pool) {
    if (e.matches('input, svg, path, img') || (opener && opener.contains(e))) continue;
    const r = e.getBoundingClientRect();
    const t = clean(e.innerText);
    if (!t || t.length > 80 || r.width === 0 || r.height === 0 || /^(Сбросить|Готово|Применить|Показать.*)$/i.test(t)) continue;
    // кликаем по самому флажку/варианту, а не по вложенному тексту
    const target = e.closest('[role=checkbox], [role=option], label') || e;
    if (seenText.has(t)) continue;
    seenText.add(t);
    target.setAttribute('data-mcp-opt', String(out.length));
    out.push({text: t, role: target.getAttribute('role'), marker: target.getAttribute('data-marker'),
              checked: target.getAttribute('aria-checked') || target.getAttribute('aria-selected')});
  }
  return out;
}
"""


async def read_groups(page) -> list[dict[str, Any]]:
    return await page.evaluate(F.JS_EXTRACT_FILTERS, F.FILTERS_ROOT) or []


async def snapshot(page) -> dict[str, str]:
    try:
        return await page.evaluate(JS_SNAPSHOT)
    except Exception:  # noqa: BLE001 — страница могла перерисоваться
        return {"url": page.url, "count": ""}


async def wait_update(page, before: dict[str, str], timeout: float = UPDATE_TIMEOUT) -> bool:
    """Ждёт, пока после изменения фильтра сменится URL или счётчик объявлений."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        await asyncio.sleep(0.4)
        now = await snapshot(page)
        if now["url"] != before["url"] or (now["count"] and now["count"] != before["count"]):
            log.info("Выдача обновилась за %.1f с (%s -> %s)", time.monotonic() - started, before["count"], now["count"])
            await asyncio.sleep(0.5)
            browser.mark_navigation()
            return True
    log.info("Выдача не обновилась за %.0f с", timeout)
    return False


async def click_marked(page) -> None:
    loc = page.locator("[data-mcp-target]").first
    await center(loc)
    await loc.click(timeout=8000)


async def click_marker(page, marker: str, title: str | None = None, label: str | None = None) -> bool:
    if not await page.evaluate(JS_MARK, [marker, title, label]):
        return False
    before = await snapshot(page)
    await click_marked(page)
    await wait_update(page, before)
    await asyncio.sleep(FORM_PAUSE)
    return True


async def fill_input(page, marker: str, value: str | None) -> bool:
    loc = page.locator(f'[data-marker="{marker}"]').first
    if not await loc.count():
        return False
    before = await snapshot(page)
    await center(loc)
    await loc.click(timeout=8000)
    await loc.press("Control+A")
    await loc.press("Backspace")
    if value:
        await loc.type(value, delay=50)
    await loc.press("Tab")
    await wait_update(page, before)
    await asyncio.sleep(FORM_PAUSE)
    return True


async def expand_group(page, g: dict[str, Any]) -> bool:
    """Нажимает «Показать ещё» в группе, чтобы стали видны все варианты."""
    m = (g.get("_m") or {}).get("more")
    if not m:
        return False
    loc = page.locator(f'[data-marker="{m}"]').first
    if not await loc.count():
        return False
    await center(loc)
    await loc.click(timeout=8000)
    await asyncio.sleep(1.0)
    return True


async def expand_all(page, groups: list[dict[str, Any]]) -> int:
    n = 0
    for g in groups:
        if g.get("has_more") and await expand_group(page, g):
            n += 1
            await asyncio.sleep(0.4)
    try:  # если «Показать ещё» открыло окно, закрываем его
        if await page.locator('[role=dialog]:visible').count():
            await page.keyboard.press("Escape")
    except Exception:  # noqa: BLE001
        pass
    return n


# ---------------------------------------------------------------------------
# Установка одной группы
# ---------------------------------------------------------------------------
def _values(spec: dict[str, Any]) -> list[Any]:
    v = spec.get("values", spec.get("value"))
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


async def set_checkboxes(page, g: dict[str, Any], spec: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Выставляет ровно указанные флажки группы. Возвращает (что выбрано, что не найдено)."""
    wanted = F.expand_values(g, _values(spec))
    options = g.get("options") or []
    idx, missing = [], []
    for v in wanted:
        i = F.fuzzy_index(v, options)
        if i is None and g.get("has_more"):
            await expand_group(page, g)
            g.update(next((x for x in await read_groups(page) if x["key"] == g["key"]), g))
            g["has_more"] = False
            options = g.get("options") or []
            i = F.fuzzy_index(v, options)
        if i is None:
            missing.append(str(v))
        elif i not in idx:
            idx.append(i)
    want_labels = {options[i] for i in idx}
    markers = (g.get("_m") or {}).get("opts") or [None] * len(options)
    title = g.get("section") or g.get("name")
    current = set(g.get("selected") or [])
    if spec.get("mode") != "add":  # снимаем лишние флажки
        for i, label in enumerate(options):
            if label in current and label not in want_labels:
                await click_marker(page, markers[i] if i < len(markers) else None, title, label)
    for i in idx:
        if options[i] not in current:
            await click_marker(page, markers[i] if i < len(markers) else None, title, options[i])
    return [options[i] for i in idx], missing


async def set_single_checkbox(page, g: dict[str, Any], spec: dict[str, Any]) -> bool:
    vals = _values(spec)
    want = True if not vals else str(vals[0]).strip().lower() not in {"false", "0", "нет", "no", "off", "выкл"}
    on = bool(g.get("selected"))
    if want != on:
        marker = ((g.get("_m") or {}).get("opts") or [None])[0]
        await click_marker(page, marker, g.get("section"), (g.get("options") or [None])[0])
    return want


async def set_switch(page, g: dict[str, Any], spec: dict[str, Any]) -> str | None:
    vals = _values(spec)
    if not vals:
        return None
    options = g.get("options") or []
    i = F.fuzzy_index(vals[0], options)
    if i is None:
        return None
    if options[i] not in (g.get("selected") or []):
        marker = ((g.get("_m") or {}).get("opts") or [None] * len(options))[i]
        await click_marker(page, marker, g.get("name"), options[i])
    return options[i]


async def set_list(page, g: dict[str, Any], spec: dict[str, Any]) -> str | None:
    vals = _values(spec)
    options = g.get("options") or []
    i = F.fuzzy_index(vals[0], options) if vals else None
    sel = (g.get("_m") or {}).get("select")
    if i is None or not sel:
        return None
    if options[i] not in (g.get("selected") or []):
        before = await snapshot(page)
        loc = page.locator(f'[data-marker="{sel}"]').first
        await center(loc)
        await loc.select_option(label=options[i])
        await wait_update(page, before)
        await asyncio.sleep(FORM_PAUSE)
    return options[i]


async def set_range(page, g: dict[str, Any], spec: dict[str, Any]) -> dict[str, str]:
    m = g.get("_m") or {}
    done: dict[str, str] = {}
    for side in ("from", "to"):
        if side not in spec:
            continue
        num = F.parse_number(spec[side])
        if spec[side] not in (None, "") and num is None:
            continue
        text = F.fmt_number(num) if num is not None else ""
        if m.get(side) and await fill_input(page, m[side], text):
            done[side] = text
    return done


async def set_text(page, g: dict[str, Any], spec: dict[str, Any]) -> str | None:
    vals = _values(spec)
    marker = (g.get("_m") or {}).get("text")
    if not marker:
        return None
    text = " ".join(str(v) for v in vals)
    await fill_input(page, marker, text)
    return text


async def set_multiselect(page, g: dict[str, Any], spec: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Мультиселект с поиском («Производитель», «Модель»): ввести название, выбрать из списка."""
    opener = (g.get("_m") or {}).get("multi")
    chosen, missing = [], []
    if not opener:
        return chosen, [str(v) for v in _values(spec)]
    for v in _values(spec):
        already = " ".join([str(g.get("text") or "")] + list(g.get("selected") or []))
        if F._n(v) and F._n(v) in F._n(already):  # уже выбрано (Авито само выставило по тексту запроса)
            chosen.append(str(v))
            continue
        await page.evaluate(JS_REMEMBER_DOM)
        loc = page.locator(f'[data-marker="{opener}"]').first
        await center(loc)
        await loc.click(timeout=8000)
        await asyncio.sleep(0.8)
        inp = page.locator(f'[data-marker="{opener}/input"]').first
        if await inp.count():
            await inp.press("Control+A")
            await inp.press("Backspace")
            await inp.type(str(v), delay=60)
        else:
            await page.keyboard.type(str(v), delay=60)
        await asyncio.sleep(1.5)
        opts = await page.evaluate(JS_DROPDOWN_OPTIONS, opener)
        texts = [o["text"] for o in opts]
        i = F.fuzzy_index(v, texts)
        log.info("Мультиселект %s: ищу %r среди %s -> %s", g.get("name"), v, texts[:12], i)
        if i is None:
            missing.append(str(v))
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            continue
        await debug_dump(page, f"ms_{g['key']}_typed", {"options": opts[:30],
                         "chain": await page.evaluate(JS_CHAIN, f'[data-mcp-opt="{i}"]')})
        before = await snapshot(page)
        target = page.locator(f'[data-mcp-opt="{i}"]').first
        await center(target)
        await target.click(timeout=8000)
        updated = await wait_update(page, before, timeout=6.0)
        await debug_dump(page, f"ms_{g['key']}_clicked", {"updated": updated, "url": page.url})
        if not updated:
            # выбор мог примениться только после закрытия списка — закрываем кликом мимо, а не Escape
            before = await snapshot(page)
            await page.mouse.click(5, 300)
            updated = await wait_update(page, before, timeout=6.0)
            await debug_dump(page, f"ms_{g['key']}_closed", {"updated": updated, "url": page.url})
        else:
            await page.keyboard.press("Escape")
        group = next((x for x in await read_groups(page) if x["key"] == g["key"]), {})
        if updated or F._n(v) in F._n(" ".join([str(group.get("text") or "")] + list(group.get("selected") or []))):
            chosen.append(texts[i])
        else:
            missing.append(str(v))
        await asyncio.sleep(FORM_PAUSE)
    return chosen, missing


# ---------------------------------------------------------------------------
# Применение списка фильтров и проверка
# ---------------------------------------------------------------------------
def _spec_name(spec: dict[str, Any]) -> str:
    return str(spec.get("name") or spec.get("filter") or spec.get("key") or "").strip()


def _describe(spec: dict[str, Any]) -> str:
    parts = []
    vals = _values(spec)
    if vals:
        parts.append(", ".join(str(v) for v in vals))
    if spec.get("from") not in (None, ""):
        parts.append(f"от {spec['from']}")
    if spec.get("to") not in (None, ""):
        parts.append(f"до {spec['to']}")
    return " ".join(parts) or "вкл."


async def apply_filters(page, specs: list[dict[str, Any]], started: float) -> dict[str, Any]:
    applied: list[dict[str, Any]] = []
    not_applied: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    groups = await read_groups(page)
    if not groups:
        return {"not_applied": [{"name": _spec_name(s), "reason": "На странице нет панели фильтров."} for s in specs]}

    for n, spec in enumerate(specs):
        if not isinstance(spec, dict):
            not_applied.append({"name": str(spec), "reason": "Ожидался объект {name, values | value | from | to}."})
            continue
        if time.monotonic() - started + ACTION_COST > TIME_BUDGET - 4:
            remaining = specs[n:]
            break
        name = _spec_name(spec)
        if any(w in F._n(name) for w in F.LOCATION_WORDS) and not F.find_group(groups, name):
            not_applied.append({"name": name, "reason": "Район, метро и адрес выставляются через avito_set_location."})
            continue
        g = F.find_group(groups, name)
        if g is None:
            not_applied.append({"name": name, "reason": "Такого фильтра нет в этой категории.",
                                "similar": F.similar(name, [F.group_name(x) for x in groups])})
            continue
        gname = F.group_name(g)
        t = g.get("type")
        try:
            if t == "location":
                not_applied.append({"name": gname, "reason": "Район, метро и адрес выставляются через avito_set_location."})
                continue
            if t == "checkboxes":
                chosen, missing = await set_checkboxes(page, g, spec)
                if chosen:
                    applied.append({"name": gname, "values": chosen, "_spec": spec, "_key": g["key"]})
                if missing:
                    not_applied.append({"name": gname, "values": missing, "reason": "Нет таких вариантов.",
                                        "options": (g.get("options") or [])[:F.OPTIONS_LIMIT]})
            elif t == "checkbox":
                on = await set_single_checkbox(page, g, spec)
                applied.append({"name": gname, "value": on, "_spec": spec, "_key": g["key"]})
            elif t == "switch":
                v = await set_switch(page, g, spec)
                if v:
                    applied.append({"name": gname, "value": v, "_spec": spec, "_key": g["key"]})
                else:
                    not_applied.append({"name": gname, "value": _describe(spec), "reason": "Нет такого варианта.",
                                        "options": g.get("options")})
            elif t == "list":
                v = await set_list(page, g, spec)
                if v:
                    applied.append({"name": gname, "value": v, "_spec": spec, "_key": g["key"]})
                else:
                    not_applied.append({"name": gname, "value": _describe(spec), "reason": "Нет такого варианта.",
                                        "options": (g.get("options") or [])[:F.OPTIONS_LIMIT]})
            elif t == "range":
                if spec.get("from") in (None, "") and spec.get("to") in (None, "") and _values(spec):
                    spec = {**spec, "from": _values(spec)[0]}  # {"name": "Площадь", "value": 30} -> от 30
                done = await set_range(page, g, spec)
                if done:
                    applied.append({"name": gname, **done, "_spec": spec, "_key": g["key"]})
                else:
                    not_applied.append({"name": gname, "reason": "Для диапазона нужны числа from и/или to."})
            elif t == "text":
                v = await set_text(page, g, spec)
                applied.append({"name": gname, "value": v, "_spec": spec, "_key": g["key"]})
            elif t == "multiselect":
                chosen, missing = await set_multiselect(page, g, spec)
                if chosen:
                    applied.append({"name": gname, "values": chosen, "_spec": spec, "_key": g["key"]})
                if missing:
                    not_applied.append({"name": gname, "values": missing,
                                        "reason": "Не нашёл в выпадающем списке Авито."})
            else:
                not_applied.append({"name": gname, "reason": f"Тип фильтра «{t}» не поддерживается."})
        except Exception as e:  # noqa: BLE001
            log.exception("Ошибка при установке фильтра %s", gname)
            not_applied.append({"name": gname, "reason": f"Ошибка: {type(e).__name__}: {e}"[:300]})
        # после изменений панель могла перерисоваться (появились новые группы, например «Модель»)
        groups = await read_groups(page) or groups

    # Проверка: перечитываем панель и сверяем каждое значение
    await asyncio.sleep(0.8)
    final = {g["key"]: g for g in await read_groups(page)}
    checked: list[dict[str, Any]] = []
    for a in applied:
        g = final.get(a.pop("_key"))
        spec = a.pop("_spec")
        ok, now = _verify(a, g)
        if ok:
            checked.append(a)
        else:
            not_applied.append({**a, "reason": "Авито не приняло значение после применения.", "now": now})
    return {"applied": checked, "not_applied": not_applied, "remaining": remaining}


def _verify(a: dict[str, Any], g: dict[str, Any] | None) -> tuple[bool, Any]:
    if g is None:
        return False, "фильтр пропал со страницы"
    sel = g.get("selected") or []
    if "values" in a and g.get("type") == "checkboxes":
        return set(a["values"]) <= set(sel), sel
    if "values" in a:  # мультиселект: выбор уже проверен в set_multiselect по обновлению выдачи
        return True, g.get("text")
    if g.get("type") == "checkbox":
        return bool(sel) == bool(a.get("value")), bool(sel)
    if g.get("type") == "range":
        ok = all(F.same_number(a[s], g.get(s)) if a.get(s) else not g.get(s) for s in ("from", "to") if s in a)
        return ok, {"from": g.get("from"), "to": g.get("to")}
    if g.get("type") in ("switch", "list"):
        return a.get("value") in sel, sel
    if g.get("type") == "text":
        return F._n(a.get("value")) == F._n(g.get("text")), g.get("text")
    return True, None
