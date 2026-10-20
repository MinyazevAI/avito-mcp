#!/usr/bin/env python3
"""
Исследование живого сайта Авито для avito-mcp (этап 1).

Запуск по одному сценарию/фазе за раз:
  .venv/Scripts/python.exe research/explore.py serp flats|phones|services
  .venv/Scripts/python.exe research/explore.py card flats|phones|services
  .venv/Scripts/python.exe research/explore.py url <url> [<url> ...]
  .venv/Scripts/python.exe research/explore.py apply flats|phones|services
  .venv/Scripts/python.exe research/explore.py city

Правила: видимый Edge с профилем research/browser-profile, пауза между загрузками не меньше 5 с
(общая для всех запусков, через файл research/.last_load), счётчик загрузок в research/loads.log.
Кликаем только в строке поиска, панели фильтров, выборе города и пагинации.
При капче — не ждём ввода, а опрашиваем страницу раз в 5 с до 5 минут.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PROFILE = HERE / "browser-profile"
DUMPS = HERE / "dumps"
SHOTS = HERE / "screenshots"
FIXTURES = ROOT / "tests" / "fixtures"
LAST_LOAD = HERE / ".last_load"
LOADS_LOG = HERE / "loads.log"
DELAY = 8.0

SCENARIOS = {
    "flats": "https://www.avito.ru/kazan/kvartiry/prodam",
    "phones": "https://www.avito.ru/kazan/telefony/mobile-ASgBAgICAUSwwQ2I_Dc",
    "services": "https://www.avito.ru/kazan/predlozheniya_uslug/remont_i_obsluzhivanie_tehniki-ASgBAgICAUSYC7T3AQ",
}

BLOCK_MARKERS = ("доступ ограничен", "проблема с ip", "не робот", "подтвердите, что вы", "captcha")


def err(*a) -> None:
    print(*a, file=sys.stderr, flush=True)


def out(*a) -> None:
    print(*a, flush=True)


# ---------------------------------------------------------------------------
# JS-помощники
# ---------------------------------------------------------------------------
JS_MARKERS = r"""
(rootSel) => {
  const root = rootSel ? document.querySelector(rootSel) : document;
  if (!root) return null;
  const m = {};
  for (const el of root.querySelectorAll('[data-marker]')) {
    const k = el.getAttribute('data-marker').replace(/\d{6,}/g, 'N');
    if (!m[k]) m[k] = {count: 0, tag: el.tagName.toLowerCase(), role: el.getAttribute('role'),
                       text: (el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 100)};
    m[k].count++;
  }
  return m;
}
"""

# Все элементы управления на странице (или внутри rootSel) с цепочкой data-marker предков.
JS_CONTROLS = r"""
(rootSel) => {
  const root = rootSel ? document.querySelector(rootSel) : document;
  if (!root) return null;
  const sel = 'input, select, textarea, button, [role=checkbox], [role=radio], [role=combobox], [role=switch], [role=listbox], [role=option], [role=button], [aria-haspopup]';
  const res = [];
  for (const el of root.querySelectorAll(sel)) {
    const chain = [];
    let p = el;
    while (p && chain.length < 5) {
      if (p.getAttribute && p.getAttribute('data-marker')) chain.push(p.getAttribute('data-marker'));
      p = p.parentElement;
    }
    const lab = el.closest('label');
    const r = el.getBoundingClientRect();
    res.push({
      tag: el.tagName.toLowerCase(), type: el.getAttribute('type'), role: el.getAttribute('role'),
      name: el.getAttribute('name'), placeholder: el.getAttribute('placeholder'),
      aria: el.getAttribute('aria-label'), value: el.value !== undefined ? String(el.value).slice(0, 60) : null,
      checked: el.checked === undefined ? el.getAttribute('aria-checked') : el.checked,
      text: (el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 80),
      label: lab ? (lab.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 80) : null,
      markers: chain, visible: r.width > 0 && r.height > 0,
    });
  }
  return res;
}
"""

# Ищем контейнер панели фильтров и возвращаем селектор-метку (ставим атрибут data-explore-root).
JS_FIND_FILTERS = r"""
() => {
  document.querySelectorAll('[data-explore-root]').forEach(e => e.removeAttribute('data-explore-root'));
  const tryMark = (el, how) => { if (!el) return null; el.setAttribute('data-explore-root', how); return how; };
  for (const s of ['[data-marker="search-filters"]', '[data-marker="filters"]', '[data-marker*="search-filters"]',
                   '[data-marker*="filters/"]', 'form[data-marker]']) {
    const el = document.querySelector(s);
    if (el && el.querySelectorAll('input, [role=checkbox], [role=radio]').length >= 3) return tryMark(el, 'sel:' + s);
  }
  const btn = Array.from(document.querySelectorAll('button, a, [role=button]'))
    .find(b => /^\s*Показать\s+[\d\s ]*\s*(объявлен|предложен|результат)/i.test(b.innerText || ''));
  if (btn) {
    let p = btn.parentElement;
    while (p && p.querySelectorAll('input, [role=checkbox], [role=radio]').length < 5) p = p.parentElement;
    if (p) return tryMark(p, 'by-submit-button');
  }
  return null;
}
"""

# Обрезанный HTML: без скриптов, стилей, svg-содержимого, data:-картинок; без данных продавца.
JS_TRIM = r"""
(args) => {
  const [rootSel, maxItems, scrubAddress] = args;
  const src = rootSel ? document.querySelector(rootSel) : document.documentElement;
  if (!src) return null;
  // имена продавцов запоминаем, чтобы вычистить их и из остального текста (описания, заголовка)
  const names = new Set();
  document.querySelectorAll('[data-marker="seller-info/name"], [data-marker="seller-link/link"], ' +
                            '[data-marker="seller-info/contact-person"], a[href*="seller_info"], ' +
                            'a[href*="/brands/"], a[href*="/user/"]').forEach(e => {
    (e.innerText || '').split('\n').map(s => s.trim()).filter(s => s.length > 2 && /[A-Za-zА-Яа-яЁё]{2,}/.test(s) && s !== 'Контактное лицо')
      .forEach(s => names.add(s));
  });
  const root = src.cloneNode(true);
  root.querySelectorAll('script, style, noscript, iframe, link, template, canvas, video, picture source').forEach(e => e.remove());
  root.querySelectorAll('[data-marker^="seller-info"], [data-marker^="seller-link"], [data-marker^="sellerRate"], ' +
                        '[data-marker="sellerInfo"], [data-marker="item-view/item-view-contacts"] button, ' +
                        '[data-marker="item-view/item-view-contacts"] [data-marker*="messenger"], ' +
                        '[data-marker^="icebreakers"], [data-marker^="onboarding/services-seller"], ' +
                        '[data-marker*="favorite-sellers"]').forEach(e => e.remove());
  root.querySelectorAll('a[href*="seller_info"], a[href*="/brands/"], a[href*="/user/"]')
    .forEach(e => (e.closest('p') || e).remove());
  root.querySelectorAll('svg').forEach(e => { e.innerHTML = ''; });
  // данные продавцов и контакты
  root.querySelectorAll('[data-marker*="seller" i], [data-marker*="phone" i], [data-marker*="contact" i]:not([data-marker="item-view/item-view-contacts"]), ' +
                        '[data-marker*="avatar" i], [data-marker*="messenger" i], [data-marker*="author" i], ' +
                        '[class*="seller" i], [itemprop="seller"], [data-marker^="item-user" i], ' +
                        '[data-marker*="item-phone" i], [data-marker^="item-view/seller" i]')
    .forEach(e => e.remove());
  // отзывы (имена авторов и мастеров)
  root.querySelectorAll('[data-marker^="ratings-carousel"], [data-marker^="review" i], [data-marker*="/review" i]').forEach(e => e.remove());
  // фото: никаких ссылок на картинки объявлений
  root.querySelectorAll('img').forEach(e => { e.setAttribute('src', ''); e.setAttribute('alt', '');
                                              e.removeAttribute('srcset'); });
  root.querySelectorAll('*').forEach(e => {
    for (const a of Array.from(e.attributes)) {
      if (!/img\.avito\.st|avito\.st\/image/i.test(a.value)) continue;
      if (a.name === 'data-marker') e.setAttribute(a.name, a.value.replace(/-https?:\/\/\S*$/, ''));
      else e.removeAttribute(a.name);
    }
  });
  // адреса исполнителей услуг (их адрес — адрес продавца) и координаты на карте
  if (scrubAddress) {
    const fake = 'Казань, ул. Примерная, 1';
    root.querySelectorAll('[data-marker="item-address"], [data-marker="item-location"]').forEach(e => { e.textContent = fake; });
    root.querySelectorAll('h2, h3').forEach(h => {
      if (!/^\s*Расположение\s*$/.test(h.textContent || '') || !h.parentElement) return;
      Array.from(h.parentElement.childNodes).forEach(n => { if (n !== h && !(n.querySelector && n.querySelector('[data-marker="item-map-wrapper"]'))) n.remove(); });
      h.insertAdjacentText('afterend', fake);
    });
    root.querySelectorAll('[data-marker="item-map-wrapper"]').forEach(e => { e.setAttribute('data-map-lat', '55.796'); e.setAttribute('data-map-lon', '49.106'); });
  }
  root.querySelectorAll('*').forEach(e => { e.removeAttribute('style');
    for (const a of Array.from(e.attributes)) if (a.name.startsWith('on') || a.value.length > 400) e.removeAttribute(a.name); });
  if (maxItems) {
    const items = root.querySelectorAll('[data-marker="item"]');
    items.forEach((e, i) => { if (i >= maxItems) e.remove(); });
  }
  // имена продавцов и маскированные телефоны — из текста и атрибутов
  // имя ищем и в «замаскированном» виде: продавцы подменяют кириллицу латинскими двойниками (Мастеp Cеpгей)
  const twins = {'а': 'aа', 'с': 'cс', 'е': 'eе', 'о': 'oо', 'р': 'pр', 'х': 'xх', 'у': 'yу', 'к': 'kк', 'м': 'mм',
                 'А': 'AА', 'В': 'BВ', 'С': 'CС', 'Е': 'EЕ', 'Н': 'HН', 'К': 'KК', 'М': 'MМ', 'О': 'OО', 'Р': 'PР',
                 'Т': 'TТ', 'Х': 'XХ'};
  for (const [cyr, both] of Object.entries(Object.assign({}, twins))) twins[both[0]] = both;  // и в обратную сторону
  const esc = (ch) => ch.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const nameRes = Array.from(names).map(n => new RegExp(Array.from(n).map(ch => twins[ch] ? '[' + twins[ch] + ']' : esc(ch)).join(''), 'g'));
  const scrub = (s) => {
    for (const re of nameRes) s = s.replace(re, 'Продавец');
    return s.replace(/(\+7|\b8)[\s\-(]*\d{3}[\s\-)]*(\d{3}|XXX)[\s\-]*(\d{2}|XX)[\s\-]*(\d{2}|XX)/g, '8 XXX XXX-XX-XX');
  };
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let t = walker.nextNode(); t; t = walker.nextNode()) t.nodeValue = scrub(t.nodeValue);
  root.querySelectorAll('[title], [alt], [aria-label], [content]').forEach(e => {
    for (const a of ['title', 'alt', 'aria-label', 'content']) if (e.hasAttribute(a)) e.setAttribute(a, scrub(e.getAttribute(a)));
  });
  return root.outerHTML;
}
"""

JS_ITEMS = r"""
() => Array.from(document.querySelectorAll('[data-marker="item"]')).slice(0, 10).map(el => {
  const t = el.querySelector('[data-marker="item-title"]');
  const a = t ? (t.tagName === 'A' ? t : t.closest('a[href]') || t.querySelector('a[href]')) : el.querySelector('a[href]');
  return {title: t ? (t.innerText || t.getAttribute('title') || '').trim() : null, url: a ? a.href : null};
})
"""

JS_PAGE_INFO = r"""
() => {
  const q = (s) => document.querySelector(s);
  const txt = (e) => e ? (e.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 200) : null;
  const h1 = q('h1');
  return {
    url: location.href, title: document.title,
    h1: txt(h1),
    count: txt(q('[data-marker="page-title/count"]')),
    items: document.querySelectorAll('[data-marker="item"]').length,
    breadcrumbs: txt(q('[data-marker="breadcrumbs"]')) || txt(q('[itemtype*="BreadcrumbList"]')),
    location: txt(q('[data-marker="search-form/change-location"]')) || txt(q('[data-marker*="location"]')),
    search_input: (q('[data-marker="search-form/suggest"] input') || q('input[type="search"]') || q('input[data-marker*="search"]') || {}).value || null,
  };
}
"""


# ---------------------------------------------------------------------------
# Браузер
# ---------------------------------------------------------------------------
class Session:
    def __init__(self, pw):
        self.pw = pw
        self.ctx = None
        self.page = None
        self.loads = 0

    async def start(self):
        PROFILE.mkdir(parents=True, exist_ok=True)
        self.ctx = await self.pw.chromium.launch_persistent_context(
            str(PROFILE), channel="msedge", headless=False, locale="ru-RU",
            viewport={"width": 1366, "height": 900})
        self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()

    async def pace(self):
        try:
            last = float(LAST_LOAD.read_text())
        except Exception:  # noqa: BLE001
            last = 0.0
        wait = DELAY - (time.time() - last)
        if wait > 0:
            await asyncio.sleep(wait)

    def mark_load(self, what: str):
        LAST_LOAD.write_text(str(time.time()))
        self.loads += 1
        with LOADS_LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')}\t{what}\n")
        err(f"[load #{self.loads}] {what}")

    async def goto(self, url: str):
        await self.pace()
        self.mark_load(url)
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            err("goto timeout, продолжаю")
        await self.settle()
        if await self.wait_if_blocked():
            # после прохождения проверки страница сама не перерисовывается — открываем URL заново
            await self.pace()
            self.mark_load("reload after check: " + url)
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await self.settle()
            await self.wait_if_blocked()
        await self.wait_content()

    async def settle(self):
        try:
            await self.page.wait_for_load_state("load", timeout=10000)
        except PWTimeout:
            pass
        await asyncio.sleep(2.0)

    async def wait_content(self, timeout: float = 25.0):
        """Страницы Авито дорисовываются на клиенте: ждём выдачу, заголовок карточки или h1."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            ok = await self.page.evaluate(
                "() => !!(document.querySelector('[data-marker=\"item\"], [data-marker^=\"item-view\"], h1'))")
            if ok:
                await asyncio.sleep(1.5)
                err(f"content ready in {time.monotonic() - t0:.1f}s")
                return True
            await asyncio.sleep(0.5)
        err("content NOT ready after timeout")
        return False

    async def blocked(self) -> bool:
        try:
            t = (await self.page.title() or "").lower()
            # Проверка бывает отдельной страницей, а бывает слоем поверх обычной выдачи
            # (portals-container > [role=region]) — поэтому смотрим весь текст, а не начало.
            b = (await self.page.evaluate("() => document.body ? document.body.innerText : ''")).lower()
        except Exception:  # noqa: BLE001
            return False
        return any(m in t or m in b for m in BLOCK_MARKERS)

    async def wait_if_blocked(self):
        if not await self.blocked():
            return False
        await self.page.bring_to_front()
        try:
            SHOTS.mkdir(parents=True, exist_ok=True)
            await self.page.screenshot(path=str(SHOTS / f"blocked_{time.strftime('%H%M%S')}.png"))
            info = await self.page.evaluate(
                "() => ({url: location.href, title: document.title, h1: (document.querySelector('h1') || {}).innerText || null,"
                " region: Array.from(document.querySelectorAll('[role=region], [role=dialog]')).map(e => e.innerText.slice(0, 200)),"
                " text: (document.body ? document.body.innerText : '').slice(0, 300)})")
            err("blocked page:", json.dumps(info, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            err("blocked screenshot failed:", e)
        out("BLOCKED: Авито показало проверку. Пройдите её в окне Edge (жду до 5 минут).")
        err("!!! Капча / доступ ограничен. Пройдите проверку в окне Edge — опрашиваю раз в 5 с, до 5 минут.")
        # Страницу во время ожидания не перезагружаем: перезагрузка сбрасывает капчу, которую решает пользователь.
        for _ in range(60):
            await asyncio.sleep(5)
            if not await self.blocked():
                out("UNBLOCKED: проверка пройдена, продолжаю.")
                await self.settle()
                return True
        out("BLOCKED_TIMEOUT: проверка не пройдена за 5 минут, выхожу.")
        raise SystemExit(3)

    async def close(self):
        await self.ctx.close()


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


async def shot(s: Session, name: str, full: bool = False):
    SHOTS.mkdir(parents=True, exist_ok=True)
    try:
        await s.page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=full, timeout=20000)
    except Exception as e:  # noqa: BLE001
        err("screenshot failed:", e)


async def dump_filters(s: Session, sc: str, suffix: str):
    how = await s.page.evaluate(JS_FIND_FILTERS)
    err(f"filters root: {how}")
    root = '[data-explore-root]' if how else None
    d = DUMPS / sc
    save_json(d / f"filters_markers{suffix}.json", await s.page.evaluate(JS_MARKERS, root))
    save_json(d / f"filters_controls{suffix}.json", await s.page.evaluate(JS_CONTROLS, root))
    if how:
        html = await s.page.evaluate(JS_TRIM, ['[data-explore-root]', 0])
        save_text(d / f"filters{suffix}.html", html)
    return how


async def click_text(s: Session, pattern: str, scope: str | None = None) -> str | None:
    """Кликает первый видимый button/a/[role=button] с текстом по регулярке. Только для панели фильтров."""
    loc = s.page.locator(f"{scope} :is(button, a, [role=button], span)" if scope else ":is(button, a, [role=button])")
    loc = loc.filter(has_text=re.compile(pattern, re.I))
    n = await loc.count()
    for i in range(n):
        el = loc.nth(i)
        if await el.is_visible():
            t = (await el.inner_text()).strip()
            await el.scroll_into_view_if_needed()
            await el.click()
            await asyncio.sleep(2.0)
            return t
    return None


# ---------------------------------------------------------------------------
# Фазы
# ---------------------------------------------------------------------------
async def phase_serp(s: Session, sc: str):
    await s.goto(SCENARIOS[sc])
    d = DUMPS / sc
    info = await s.page.evaluate(JS_PAGE_INFO)
    save_json(d / "serp_info.json", info)
    out("SERP:", json.dumps(info, ensure_ascii=False))
    await shot(s, f"{sc}_serp")
    await shot(s, f"{sc}_serp_full", full=True)
    save_json(d / "serp_markers.json", await s.page.evaluate(JS_MARKERS, None))
    save_json(d / "serp_controls.json", await s.page.evaluate(JS_CONTROLS, None))
    save_json(d / "items.json", await s.page.evaluate(JS_ITEMS))
    save_text(d / "serp_full.html", await s.page.content())
    save_text(FIXTURES / f"{sc}_serp.html", await s.page.evaluate(JS_TRIM, [None, 6]))

    how = await dump_filters(s, sc, "")
    if how:
        save_text(FIXTURES / f"{sc}_filters.html", await s.page.evaluate(JS_TRIM, ['[data-explore-root]', 0]))
    # Раскрыть «Все фильтры» / «Ещё фильтры»
    clicked = await click_text(s, r"^\s*(Все|Ещё|Еще|Больше|Дополнительные)\s+фильтр")
    out("EXPAND:", clicked)
    if clicked:
        await shot(s, f"{sc}_filters_expanded")
        await shot(s, f"{sc}_filters_expanded_full", full=True)
        save_json(d / "after_expand_markers.json", await s.page.evaluate(JS_MARKERS, None))
        dlg = await s.page.evaluate("() => { const d = document.querySelector('[role=dialog], [aria-modal=true]');"
                                    " if (d) { d.setAttribute('data-explore-dialog', '1'); return true; } return false; }")
        out("DIALOG:", dlg)
        if dlg:
            save_json(d / "dialog_markers.json", await s.page.evaluate(JS_MARKERS, '[data-explore-dialog]'))
            save_json(d / "dialog_controls.json", await s.page.evaluate(JS_CONTROLS, '[data-explore-dialog]'))
            html = await s.page.evaluate(JS_TRIM, ['[data-explore-dialog]', 0])
            save_text(d / "dialog.html", html)
            save_text(FIXTURES / f"{sc}_filters_all.html", html)
        else:
            await dump_filters(s, sc, "_expanded")
    out("URL_AFTER:", s.page.url)


async def phase_card(s: Session, sc: str):
    items = json.loads((DUMPS / sc / "items.json").read_text(encoding="utf-8"))
    url = next(i["url"] for i in items if i.get("url"))
    await s.goto(url)
    d = DUMPS / sc
    info = await s.page.evaluate(JS_PAGE_INFO)
    out("CARD:", json.dumps(info, ensure_ascii=False))
    await shot(s, f"{sc}_card")
    save_json(d / "card_markers.json", await s.page.evaluate(JS_MARKERS, None))
    save_text(d / "card_full.html", await s.page.content())
    save_text(FIXTURES / f"{sc}_card.html", await s.page.evaluate(JS_TRIM, [None, 0]))


async def phase_url(s: Session, urls: list[str]):
    res = []
    for u in urls:
        await s.goto(u)
        info = await s.page.evaluate(JS_PAGE_INFO)
        info["requested"] = u
        info["first_titles"] = [i["title"] for i in (await s.page.evaluate(JS_ITEMS))[:5]]
        info["first_prices"] = await s.page.evaluate(
            "() => Array.from(document.querySelectorAll('[data-marker=\"item\"] meta[itemprop=\"price\"]'))"
            ".slice(0, 8).map(m => m.getAttribute('content'))")
        info["search_value"] = await s.page.evaluate(
            "() => (document.querySelector('[data-marker=\"search-form/suggest/input\"]') || {}).value || null")
        out("URL:", json.dumps(info, ensure_ascii=False))
        n = len(list(DUMPS.glob("url_*.html"))) + 1
        save_text(DUMPS / f"url_{n}.html", await s.page.content())
        info["saved"] = f"url_{n}.html"
        res.append(info)
    prev = []
    p = DUMPS / "url_checks.json"
    if p.exists():
        prev = json.loads(p.read_text(encoding="utf-8"))
    save_json(p, prev + res)


async def main():
    if len(sys.argv) < 2:
        err(__doc__)
        return 2
    phase = sys.argv[1]
    async with async_playwright() as pw:
        s = Session(pw)
        await s.start()
        try:
            if phase == "serp":
                await phase_serp(s, sys.argv[2])
            elif phase == "card":
                await phase_card(s, sys.argv[2])
            elif phase == "url":
                await phase_url(s, sys.argv[2:])
            else:
                mod = __import__("explore_steps")  # фазы apply/city дописываются после первого обзора
                await getattr(mod, "phase_" + phase)(s, *sys.argv[2:])
        finally:
            out(f"DONE: phase={phase} loads={s.loads}")
            await s.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
