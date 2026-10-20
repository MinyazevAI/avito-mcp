"""
Панель фильтров Авито ([data-marker="search-filters"]): разбор в компактный список групп.
Разметка (см. research/NOTES.md): заголовок группы — <h5>; флажки params[ID]/checkbox/VALUE,
переключатели params[ID]/option(VALUE), списки params[ID]/select, диапазоны params[ID]-from/-to/input,
цена price-from/-to/input, мультиселект params[ID]/multiselect-outline-input, место geoFilters/select.
Фильтры на Авито применяются сразу после изменения, отдельной кнопки «Показать N» нет.
"""
from __future__ import annotations

import re
from typing import Any

from parsing import clean

FILTERS_ROOT = '[data-marker="search-filters"]'

# Возвращает список групп: {name, key, type, options, values, selected, from, to, text, placeholder, has_more}
JS_EXTRACT_FILTERS = r"""
(rootSel) => {
  const root = (rootSel && document.querySelector(rootSel)) || document.querySelector('[data-marker="search-filters"]') || document;
  const clean = (s) => (s || '').replace(/[\s ]+/g, ' ').trim();
  const txt = (e) => e ? clean(e.innerText || e.textContent) : '';
  const dm = (e) => e.getAttribute('data-marker') || '';
  const keyOf = (m) => { const r = m.match(/^(params\[\d+\]|price|[A-Za-z]+(?:\/[A-Za-z]+)?)/); return r ? r[1] : m; };

  // 1. Все «ключевые» элементы управления
  const ctrls = [];
  for (const el of root.querySelectorAll('[data-marker]')) {
    const m = dm(el);
    let kind = null, value = null;
    let r;
    if ((r = m.match(/^(params\[\d+\])\/multiselect-button-checkbox\(([^)]*)\)/))) {
      kind = 'msopt'; value = r[2];                         // вариант мультиселекта (виден, когда список открыт)
    } else if ((r = m.match(/^(.+)\/checkbox\/([^/]+)$/)) && r[2] !== 'toggle') {
      kind = 'checkbox'; value = r[2];
    } else if ((r = m.match(/^(.+)\/option\(([^)]*)\)$/)) && el.tagName === 'LABEL') {
      kind = 'radio'; value = r[2];
    } else if (/\/select$/.test(m) && el.tagName === 'SELECT') {
      kind = 'select';
    } else if ((r = m.match(/^(.+)-(from|to)\/input$/))) {
      kind = 'range_' + r[2];
    } else if (/\/multiselect-outline-input$/.test(m)) {
      kind = 'multiselect';
    } else if (/\/input$/.test(m) && el.tagName === 'INPUT') {
      if (/multiselect/.test(m)) continue;
      kind = 'text';
    } else if (m === 'geoFilters/select') {
      kind = 'geo';
    } else if (/\/show-button$/.test(m)) {
      kind = 'more';
    } else if (/^\w+\/\w+\/\w+$/.test(m) && el.tagName === 'LABEL') {
      kind = 'checkbox'; value = m.split('/').pop();      // filters/withImagesOnly/withImagesOnly
    }
    if (kind) ctrls.push({el, m, kind, value});
  }

  // 2. Заголовок группы: ближайший предыдущий h5 в пределах общего предка
  // Идём вверх до первого предка, где есть заголовок; если он там один — это заголовок группы.
  const titleFor = (el) => {
    let p = el.parentElement;
    while (p && p !== root.parentElement) {
      const hs = p.querySelectorAll('h5, h4, h3');
      if (hs.length === 1) return txt(hs[0]);
      if (hs.length > 1) return null;
      p = p.parentElement;
    }
    return null;
  };

  // 3. Группировка по ключу (params[ID], price, footWalkingMetro, …)
  const groups = new Map();
  for (const c of ctrls) {
    const key = c.kind === 'geo' ? 'geo' : c.kind === 'msopt' ? c.m.split('/')[0] : keyOf(c.m.replace(/-(from|to)\/input$/, '').replace(/\/(checkbox|option|select|input|show-button).*$/, ''));
    if (!groups.has(key)) groups.set(key, {key, title: null, kinds: new Set(), options: [], from: null, to: null, more: false, selected: [], m: {}});
    const g = groups.get(key);
    if (!g.title) g.title = titleFor(c.el);
    g.kinds.add(c.kind);
    if (c.kind === 'checkbox') {
      const on = c.el.getAttribute('aria-checked') === 'true' || !!(c.el.querySelector('input') || {}).checked;
      g.options.push({value: c.value, label: txt(c.el), marker: c.m});
      if (on) g.selected.push(txt(c.el));
    } else if (c.kind === 'radio') {
      const inp = c.el.querySelector('input');
      const on = (inp && inp.checked) || c.el.getAttribute('aria-checked') === 'true' || c.el.getAttribute('aria-pressed') === 'true';
      g.options.push({value: c.value, label: txt(c.el), marker: c.m});
      if (on) g.selected.push(txt(c.el));
    } else if (c.kind === 'select') {
      g.m.select = c.m;
      for (const o of c.el.options) if (clean(o.textContent)) g.options.push({value: o.value, label: clean(o.textContent)});
      const so = c.el.options[c.el.selectedIndex];
      if (so) g.selected.push(clean(so.textContent));
    } else if (c.kind === 'range_from') {
      g.from = c.el.value || null; g.m.from = c.m;
    } else if (c.kind === 'range_to') {
      g.to = c.el.value || null; g.m.to = c.m;
    } else if (c.kind === 'text') {
      g.text = c.el.value || null; g.placeholder = c.el.getAttribute('placeholder'); g.m.text = c.m;
    } else if (c.kind === 'msopt') {
      const label = txt(c.el);
      if (c.el.getAttribute('aria-checked') === 'true' && label && !g.selected.includes(label)) g.selected.push(label);
    } else if (c.kind === 'multiselect') {
      const inp = c.el.querySelector('input');
      g.m.multi = c.m;
      g.placeholder = inp ? inp.getAttribute('placeholder') : null;
      // выбранные значения: в поле ввода или в тексте подписи (чипы)
      const t = [inp ? inp.value : '', txt(c.el)].map(clean).filter(x => x && x !== g.placeholder).join(', ');
      if (t) g.text = t;
    } else if (c.kind === 'geo') {
      g.text = txt(c.el); g.m.geo = c.m;
    } else if (c.kind === 'more') {
      g.more = true; g.m.more = c.m;
    }
  }

  // 4. Флажки без data-marker (например, «Цвет»: div[role=checkbox]) — к группе по заголовку
  for (const el of root.querySelectorAll('[role=checkbox]:not([data-marker])')) {
    if (el.closest('[data-marker*="/checkbox/"]')) continue;
    const title = titleFor(el);
    if (!title) continue;
    let g = Array.from(groups.values()).find(x => x.title === title);
    if (!g) { g = {key: 'title:' + title, title, kinds: new Set(), options: [], from: null, to: null, more: false, selected: [], m: {}};
              groups.set(g.key, g); }
    g.kinds.add('checkbox');
    const label = txt(el);
    if (!label || g.options.some(o => o.label === label)) continue;
    g.options.push({value: label, label, marker: null});
    if (el.getAttribute('aria-checked') === 'true') g.selected.push(label);
  }

  const out = [];
  for (const g of groups.values()) {
    const k = Array.from(g.kinds).filter(x => x !== 'more');
    let type = k.includes('checkbox') ? 'checkboxes' : k.includes('radio') ? 'switch' : k.includes('select') ? 'list'
             : (k.includes('range_from') || k.includes('range_to')) ? 'range' : k.includes('geo') ? 'location'
             : k.includes('multiselect') ? 'multiselect' : 'text';
    let title = g.title, section;
    // одиночный флажок называем по подписи («С Авито Доставкой»), заголовок секции — отдельно
    if (type === 'checkboxes' && g.options.length === 1 && g.options[0].label !== title) {
      section = title || undefined; title = g.options[0].label; type = 'checkbox';
    }
    g.m.opts = g.options.map(o => o.marker);
    out.push(Object.fromEntries(Object.entries({
      name: title, section, key: g.key, type,
      options: g.options.length ? g.options.map(o => o.label) : undefined,
      values: g.options.length ? g.options.map(o => o.value) : undefined,
      selected: g.selected.length ? g.selected : undefined,
      from: g.from || undefined, to: g.to || undefined, text: g.text || undefined,
      placeholder: g.placeholder || undefined,
      has_more: g.more || undefined,
      _m: g.m,
    }).filter(([, v]) => v !== undefined)));
  }
  return out;
}
"""

TYPE_NAMES = {
    "checkboxes": "флажки", "checkbox": "флажок", "switch": "переключатель", "list": "список", "range": "диапазон от–до",
    "text": "текст", "location": "место (район, метро, адрес)", "multiselect": "список с поиском",
}
NAMES_BY_KEY = {"categoryId": "Категория", "geo": "Где искать", "price": "Цена, ₽"}


def group_name(g: dict[str, Any]) -> str:
    if g.get("name"):
        return clean(g["name"]) or g["key"]
    if g.get("key") in NAMES_BY_KEY:
        return NAMES_BY_KEY[g["key"]]
    opts = [o for o in g.get("options") or [] if o]
    return " / ".join(opts[:4]) if opts else g.get("key", "?")


def summarize(groups: list[dict[str, Any]] | None) -> list[str]:
    """Краткий список групп для ответа avito_open_search: «Название — тип»."""
    out, seen = [], set()
    for g in groups or []:
        name = group_name(g)
        if name in seen:
            continue
        seen.add(name)
        out.append(f"{name} — {TYPE_NAMES.get(g.get('type'), g.get('type'))}")
    return out


# ---------------------------------------------------------------------------
# Компактный вид для avito_get_filters
# ---------------------------------------------------------------------------
OPTIONS_LIMIT = 40


def public_group(g: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"name": group_name(g), "type": g.get("type")}
    if g.get("section"):
        out["section"] = g["section"]
    opts = g.get("options") or []
    if opts and g.get("type") != "checkbox":
        out["options"] = opts[:OPTIONS_LIMIT]
        if len(opts) > OPTIONS_LIMIT:
            out["options_total"] = len(opts)
    if g.get("has_more"):
        out["more_options"] = True
    sel = [x for x in g.get("selected") or [] if x]
    if g.get("type") == "checkbox":
        out["value"] = bool(sel)
    elif sel:
        out["value"] = sel if g.get("type") == "checkboxes" else sel[0]
    for k in ("from", "to", "text"):
        if g.get(k):
            out[k] = g[k]
    if g.get("type") == "multiselect":
        out["hint"] = "Варианты — в выпадающем списке Авито; передайте название, сервер найдёт его поиском."
    if g.get("type") == "location":
        out["hint"] = "Район, метро и адрес выставляет avito_set_location."
    return out


# ---------------------------------------------------------------------------
# Нечёткое сопоставление
# ---------------------------------------------------------------------------
def _n(s: Any) -> str:
    s = str(s if s is not None else "").lower().replace("ё", "е").replace(" ", " ")
    s = re.sub(r"[★⋅·,.;:!?()\"«»/\\₽²]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _words(s: Any) -> list[str]:
    return [w for w in _n(s).split(" ") if w]


def _prefix_match(a: str, b: str) -> bool:
    """Слова совпадают по началу: «кирпич» ~ «кирпичный», «новый» ~ «новое»."""
    if a == b:
        return True
    k = min(len(a), len(b))
    if k < 3:
        return False
    need = max(3, min(4, k - 2))
    return a[:need] == b[:need]


def fuzzy_index(target: Any, choices: list[str]) -> int | None:
    """Индекс подходящего варианта или None. Регистр, ё/е, сокращения, «1» -> «1 комната»."""
    t = _n(target)
    if not t:
        return None
    norm = [_n(c) for c in choices]
    for i, c in enumerate(norm):  # 1. точное совпадение
        if c == t:
            return i
    m = re.fullmatch(r"(\d+)\s*(?:к|к[а-я]*|комн[а-я]*|-?комнатн[а-я]*)?\+?", t)
    if m:  # 2. «1», «2к», «3 комнаты» -> вариант, который начинается с этого числа
        for i, c in enumerate(norm):
            if re.match(rf"^{m.group(1)}(?:\D|$)", c):
                return i
    for i, c in enumerate(norm):  # 3. начало строки
        if c.startswith(t + " ") or t.startswith(c + " "):
            return i
    tw = _words(t)
    for i, c in enumerate(norm):  # 4. все слова запроса есть в варианте (по началу слова)
        cw = c.split(" ")
        if tw and all(any(_prefix_match(w, x) for x in cw) for w in tw):
            return i
    for i, c in enumerate(norm):  # 5. все слова варианта есть в запросе
        cw = c.split(" ")
        if cw and all(any(_prefix_match(x, w) for w in tw) for x in cw):
            return i
    return None


def similar(target: Any, choices: list[str], limit: int = 5) -> list[str]:
    """Похожие варианты для сообщения об ошибке."""
    tw = set(_words(target))
    scored = []
    for c in choices:
        cw = _words(c)
        score = sum(1 for w in tw if any(_prefix_match(w, x) for x in cw))
        if score:
            scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:limit]]


GROUP_ALIASES = {
    "цена": "price", "стоимость": "price",
    "комнаты": "Количество комнат", "комнат": "Количество комнат", "число комнат": "Количество комнат",
    "площадь": "Общая площадь", "год постройки": "Год постройки дома", "год": "Год постройки дома",
    "бренд": "Производитель", "марка": "Производитель",
    "доставка": "С Авито Доставкой", "авито доставка": "С Авито Доставкой", "с доставкой": "С Авито Доставкой",
}
LOCATION_WORDS = ("район", "метро", "адрес", "радиус", "где искать", "местоположение")


def find_group(groups: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    if not name:
        return None
    for g in groups:  # ключ params[ID]
        if g.get("key") == name.strip():
            return g
    alias = GROUP_ALIASES.get(_n(name))
    if alias:
        for g in groups:
            if g.get("key") == alias or _n(group_name(g)) == _n(alias):
                return g
    names = [group_name(g) for g in groups]
    i = fuzzy_index(name, names)
    if i is None:
        # «Авито Доставка» -> одиночный флажок «С Авито Доставкой»
        want = _words(name)
        i = next((j for j, g in enumerate(groups) if g.get("type") == "checkbox"
                  and all(any(_prefix_match(w, x) for x in _words(group_name(g))) for w in want)), None)
    return groups[i] if i is not None else None


USED_WORDS = {"б у", "бу", "used", "подержанный", "подержанное", "не новый", "не новое", "с пробегом"}


def expand_values(g: dict[str, Any], values: list[Any]) -> list[Any]:
    """«б/у» в группе «Состояние» -> все варианты, кроме нового и требующего ремонта."""
    out: list[Any] = []
    for v in values:
        if _n(v) in USED_WORDS and "состоян" in _n(group_name(g)):
            out += [o for o in g.get("options") or [] if "нов" not in _n(o) and "ремонт" not in _n(o)]
        else:
            out.append(v)
    return out


def parse_number(v: Any) -> float | None:
    """'12 млн' -> 12000000, '30 тыс' -> 30000, '2,7' -> 2.7, 2006 -> 2006."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).lower().replace(" ", "").replace(" ", "").replace(",", ".")
    m = re.match(r"^(\d+(?:\.\d+)?)(млн|миллион[а-я]*|м|тыс[а-я]*|т|k|к)?", s)
    if not m:
        return None
    num = float(m.group(1))
    mult = m.group(2) or ""
    if mult.startswith(("млн", "миллион")) or mult == "м":
        num *= 1_000_000
    elif mult.startswith(("тыс", "т", "k", "к")):
        num *= 1_000
    return num


def fmt_number(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x).replace(".", ",")


def same_number(a: Any, b: Any) -> bool:
    x, y = parse_number(a), parse_number(b)
    return x is not None and y is not None and abs(x - y) < 1e-6
