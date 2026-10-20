"""
Разбор страниц Авито: JS-функции, которые выполняются в браузере и возвращают сырые данные,
и Python-функции, которые приводят их к компактному виду. Селекторы — по data-marker
(см. research/NOTES.md). Данные продавцов не собираем.
"""
from __future__ import annotations

import re
from typing import Any

from browser import BASE, strip_url

# ---------------------------------------------------------------------------
# Выдача
# ---------------------------------------------------------------------------
JS_SEARCH = r"""
() => {
  const root = document.querySelector('[data-marker="catalog-serp"]') || document;
  const text = (el) => (el ? (el.innerText || el.textContent || '') : '');
  return Array.from(root.querySelectorAll('[data-marker="item"]')).map((el) => {
    const q = (s) => el.querySelector(s);
    const t = q('[data-marker="item-title"]');
    let a = null;
    if (t) a = t.tagName === 'A' ? t : (t.closest('a[href]') || t.querySelector('a[href]'));
    if (!a) a = q('a[href]');
    const pm = q('meta[itemprop="price"]');
    const pe = q('[data-marker="item-price"]');
    return {
      id: el.getAttribute('data-item-id') || (el.id || '').replace(/^i/, '') || null,
      title: t ? (text(t).trim() || t.getAttribute('title')) : null,
      url: a ? a.href : null,
      price: pm ? pm.getAttribute('content') : (pe ? text(pe) : null),
      address: text(q('[data-marker="item-address"]')) || null,
      date: text(q('[data-marker="item-date"]')) || null,
    };
  });
}
"""

JS_HAS_NEXT_PAGE = r"""
() => {
  const n = document.querySelector(
    '[data-marker="pagination-button/nextPage"], [data-marker="pagination-button/next"], a[aria-label="Следующая страница"]'
  );
  if (!n) return null;
  return !(n.getAttribute('aria-disabled') === 'true' || n.hasAttribute('disabled'));
}
"""

# Сводка страницы выдачи: заголовок, число объявлений, хлебные крошки, город, строка поиска, витрина
JS_PAGE_INFO = r"""
() => {
  const q = (s) => document.querySelector(s);
  const clean = (s) => (s || '').replace(/[\s ]+/g, ' ').trim();
  const crumbs = q('[data-marker="breadcrumbs"]');
  const crumbList = crumbs ? Array.from(crumbs.querySelectorAll('a, span'))
      .filter(e => !e.querySelector('a, span')).map(e => clean(e.innerText)).filter(Boolean) : [];
  const tiles = Array.from(document.querySelectorAll('a[data-marker="category-widget/sliced-link"]'))
      .map(a => ({name: clean(a.innerText), url: a.href}))
      .filter(t => t.name);
  return {
    url: location.href,
    h1: clean((q('[data-marker="page-title/text"]') || q('h1') || {}).innerText),
    count: clean((q('[data-marker="page-title/count"]') || {}).innerText),
    items: document.querySelectorAll('[data-marker="catalog-serp"] [data-marker="item"]').length,
    breadcrumbs: crumbList,
    location: clean((q('[data-marker="search-form/change-location"]') || {}).innerText),
    query: (q('[data-marker="search-form/suggest/input"]') || {}).value || '',
    has_filters: !!q('[data-marker="search-filters"]'),
    tiles,
  };
}
"""

# ---------------------------------------------------------------------------
# Карточка объявления
# ---------------------------------------------------------------------------
JS_LISTING = r"""
() => {
  const q = (s) => document.querySelector(s);
  const text = (el) => (el ? (el.innerText || el.textContent || '').trim() : null);
  const h1 = q('[data-marker="item-view/title-info"]') || q('h1');
  const pr = q('[data-marker="item-view/item-price"]') || q('[itemprop="price"]');
  // Адрес: блок под заголовком «Расположение» (itemprop и item-address на карточках больше нет)
  let address = null;
  const addrEl = q('[itemprop="address"]') || q('[data-marker="item-view/item-address"]');
  if (addrEl) address = text(addrEl);
  if (!address) {
    const h = Array.from(document.querySelectorAll('h2, h3')).find(e => /^\s*Расположение\s*$/.test(e.innerText || ''));
    if (h && h.parentElement) {
      const t = (h.parentElement.innerText || '').replace(h.innerText, '').trim();
      address = t.split(/©|\n\s*\n/)[0].trim() || null;
    }
  }
  const map = q('[data-marker="item-map-wrapper"]');
  const desc = q('[data-marker="item-view/item-description"]') || q('[itemprop="description"]');
  const params = [];
  const seen = new Set();
  const selectors = [
    '[data-marker="item-view/item-params"] li',
    '[data-marker="item-view/house-params"] li',
    '[class*="params-paramsList"] li',
  ];
  for (const sel of selectors) {
    for (const li of document.querySelectorAll(sel)) {
      const s = (li.innerText || li.textContent || '').replace(/\s+/g, ' ').trim();
      if (s && !seen.has(s)) { seen.add(s); params.push(s); }
    }
  }
  const prices = Array.from(document.querySelectorAll('[data-marker^="services-price-list/preview/"]'))
      .map(e => (e.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
  const body = document.body ? (document.body.innerText || document.body.textContent || '') : '';
  return {
    title: text(h1),
    price: pr ? (pr.getAttribute('content') || text(pr)) : null,
    address,
    lat: map ? map.getAttribute('data-map-lat') : null,
    lon: map ? map.getAttribute('data-map-lon') : null,
    date: text(q('[data-marker="item-view/item-date"]')),
    description: text(desc),
    params,
    price_list: prices,
    body: body.slice(0, 20000),
  };
}
"""

# Параметры, которые ищем в тексте страницы, если список параметров не нашёлся по разметке
PARAM_KEYS = (
    "Количество комнат", "Общая площадь", "Жилая площадь", "Площадь кухни", "Этаж",
    "Балкон или лоджия", "Тип комнат", "Высота потолков", "Санузел", "Окна", "Ремонт", "Отделка",
    "Способ продажи", "Вид сделки", "Тип дома", "Год постройки", "Этажей в доме",
    "Пассажирский лифт", "Грузовой лифт", "Парковка", "Двор", "В доме",
)
CLOSED_MARKERS = (
    "снято с публикации", "объявление удалено", "истёк срок размещения", "истек срок размещения",
    "такой страницы нет", "не нашли такую страницу",
)

# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------
_SPACES = re.compile(r"[\s   ]+")


def clean(s: Any) -> str | None:
    if s is None:
        return None
    s = _SPACES.sub(" ", str(s)).strip()
    return s or None


def clean_lines(s: Any) -> str | None:
    if not s:
        return None
    parts = [p for p in (clean(x) for x in str(s).splitlines()) if p]
    return " · ".join(parts) or None


def price(s: Any) -> int | None:
    """'5500000' или '5 500 000 ₽' -> 5500000. Берёт первое число, а не все цифры подряд."""
    if s is None:
        return None
    m = re.search(r"\d[\d    ]*", str(s))
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(0))
    return int(digits) if digits else None


def price_from_text(text: str) -> int | None:
    """Запасной вариант: первая сумма в рублях в начале страницы (там цена объявления)."""
    m = re.search(r"(\d{1,3}(?:[    ]\d{3})+|\d{4,})\s*₽", text or "")
    return price(m.group(1)) if m else None


def count(s: Any) -> int | None:
    """'24 576' -> 24576."""
    if not s:
        return None
    digits = re.sub(r"\D", "", str(s))
    return int(digits) if digits else None


def to_float(s: Any) -> float | None:
    if not s:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", str(s))
    return float(m.group(0).replace(",", ".")) if m else None


def parse_title(title: str | None) -> dict[str, Any]:
    """'1-к. квартира, 35,5 м², 5/9 эт.' -> комнаты, площадь, этаж."""
    out: dict[str, Any] = {}
    if not title:
        return out
    t = title.lower()
    m = re.search(r"(\d+)\s*-\s*(?:к\b|комн)", t)
    if m:
        out["rooms"] = int(m.group(1))
    elif "студи" in t:
        out["rooms"] = "студия"
    elif "своб" in t and "планир" in t:
        out["rooms"] = "свободная планировка"
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*м(?:²|2)", t)
    if m:
        out["area_m2"] = float(m.group(1).replace(",", "."))
    m = re.search(r"(\d+)\s*/\s*(\d+)\s*эт", t)
    if m:
        out["floor"], out["floors_total"] = int(m.group(1)), int(m.group(2))
    return out


def params_to_dict(lines: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in lines or []:
        if ":" not in line:
            continue
        key, value = (clean(x) for x in line.split(":", 1))
        if key and value and key not in result:
            result[key] = value
    return result


def params_from_text(body: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in PARAM_KEYS:
        m = re.search(re.escape(key) + r"\s*:\s*([^\n]+)", body or "")
        if m and clean(m.group(1)):
            result[key] = clean(m.group(1))
    return result


def key_facts(params: dict[str, str]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    year = re.search(r"\b(1[89]\d\d|20\d\d)\b", params.get("Год постройки", ""))
    if year:
        facts["year_built"] = int(year.group(1))
    if params.get("Общая площадь"):
        facts["total_area_m2"] = to_float(params["Общая площадь"])
    if params.get("Площадь кухни"):
        facts["kitchen_area_m2"] = to_float(params["Площадь кухни"])
    floor = re.search(r"(\d+)\s*из\s*(\d+)", params.get("Этаж", ""))
    if floor:
        facts["floor"], facts["floors_total"] = int(floor.group(1)), int(floor.group(2))
    if params.get("Тип дома"):
        facts["house_type"] = params["Тип дома"]
    return {k: v for k, v in facts.items() if v is not None}


def drop_empty(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


# ---------------------------------------------------------------------------
# Сырые данные -> компактный ответ
# ---------------------------------------------------------------------------
def absolute(url: str | None) -> str | None:
    """Ссылка с сайта Авито в полном виде (в офлайн-разборе href бывает относительным)."""
    if not url:
        return None
    m = re.match(r"^(?:https?:)?/*(?:www\.avito\.ru)?(/.*)$", url) if "avito.ru" in url or not url.startswith("http") else None
    if m and not url.startswith(BASE):
        return BASE + m.group(1)
    return url


def search_item(r: dict[str, Any]) -> dict[str, Any]:
    title = clean(r.get("title"))
    return drop_empty({
        "id": clean(r.get("id")),
        "title": title,
        "url": strip_url(absolute(r.get("url"))),
        "price_rub": price(r.get("price")),
        "address": clean_lines(r.get("address")),
        "date": clean(r.get("date")),
        **parse_title(title),
    })


DESCRIPTION_LIMIT = 1500


def listing_details(raw: dict[str, Any], url: str) -> dict[str, Any]:
    body = raw.get("body") or ""
    params = params_to_dict(raw.get("params") or []) or params_from_text(body)
    description = clean(raw.get("description"))
    if description and len(description) > DESCRIPTION_LIMIT:
        description = description[:DESCRIPTION_LIMIT] + "…"
    head = body[:3000].lower()
    coords = None
    if raw.get("lat") and raw.get("lon"):
        coords = [to_float(raw["lat"]), to_float(raw["lon"])]
    return drop_empty({
        "url": url,
        "title": clean(raw.get("title")),
        "price_rub": price(raw.get("price")) or price_from_text(body[:3000]),
        "address": (clean_lines(raw.get("address")) or "").removesuffix("…").strip(" ·") or None,
        "coords": coords,
        "date": (clean(raw.get("date")) or "").lstrip("· ") or None,
        **key_facts(params),
        "params": params,
        "price_list": [clean(x) for x in raw.get("price_list") or []][:10],
        "description": description,
        "closed": True if any(m in head for m in CLOSED_MARKERS) else None,
    })


def page_summary(info: dict[str, Any]) -> dict[str, Any]:
    """Сводка страницы выдачи из JS_PAGE_INFO."""
    crumbs = [c for c in info.get("breadcrumbs") or [] if c and c.lower() != "главная"]
    return drop_empty({
        "title": clean(info.get("h1")),
        "count": count(info.get("count")),
        "category_path": " › ".join(crumbs) or None,
        "city": (clean(info.get("location")) or "").split(",")[0] or None,
        "query": clean(info.get("query")),
    })
