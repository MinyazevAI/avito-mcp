"""
Справочники avito-mcp: города (слаг в URL) и категории (путь в URL), нечёткое сопоставление,
сборка ссылки на выдачу.

Слаги категорий угадывать нельзя (без хвоста «-ASgB…» Авито отвечает 404), поэтому категории берём
из справочника data/categories.json — он собран из дерева категорий, которое Авито вшивает в каждую
страницу (research/build_catalog.py). Слаг города — транслитерация названия по правилам Авито;
сервер проверяет его по открытой странице, а при ошибке выбирает город через интерфейс и запоминает слаг.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from browser import BASE, PROFILE_DIR

log = logging.getLogger("avito-mcp")
HERE = Path(__file__).resolve().parent
CATEGORIES_FILE = HERE / "data" / "categories.json"
CITY_CACHE_FILE = PROFILE_DIR / "avito-mcp-cities.json"

# ---------------------------------------------------------------------------
# Нормализация текста
# ---------------------------------------------------------------------------
STOP_WORDS = {"в", "во", "на", "и", "для", "по", "с", "со", "от", "до", "из", "к", "а", "или", "все", "всё",
              "авито", "объявления", "объявление", "купить", "продажа", "продам", "куплю", "найти", "б", "у"}


def norm(s: str | None) -> str:
    s = (s or "").lower().replace("ё", "е").replace(" ", " ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-zа-я0-9\- ]+", " ", s)).strip()


def stems(s: str | None, drop_stop: bool = True) -> list[str]:
    """Грубая основа слова: первые 5 букв (квартиры/квартиру -> «кварт»)."""
    out = []
    for w in re.split(r"[\s\-]+", norm(s)):
        if not w or (drop_stop and w in STOP_WORDS):
            continue
        out.append(w[:5] if len(w) > 5 else w)
    return out


# ---------------------------------------------------------------------------
# Города
# ---------------------------------------------------------------------------
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}
# Исключения из транслитерации и синонимы. Слаги из футера Авито проверены на живом сайте.
CITY_SLUGS = {
    "москва": "moskva", "мск": "moskva",
    "санкт-петербург": "sankt-peterburg", "спб": "sankt-peterburg", "питер": "sankt-peterburg",
    "петербург": "sankt-peterburg",
    "нижний новгород": "nizhniy_novgorod", "ростов-на-дону": "rostov-na-donu", "ростов": "rostov-na-donu",
    "екатеринбург": "ekaterinburg", "новосибирск": "novosibirsk", "казань": "kazan", "челябинск": "chelyabinsk",
    "самара": "samara", "омск": "omsk",
    "spb": "sankt-peterburg", "msk": "moskva", "moscow": "moskva",
    "россия": "all", "вся россия": "all", "все регионы": "all", "любой": "all",
}


def translit_city(name: str) -> str:
    s = norm(name)
    s = "".join(_TRANSLIT.get(ch, ch) for ch in s)
    s = re.sub(r"\s+", "_", s.strip())
    return re.sub(r"[^a-z0-9_\-]", "", s)


def _load_city_cache() -> dict[str, str]:
    try:
        return json.loads(CITY_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — кэша может не быть
        return {}


def remember_city(name: str, slug: str) -> None:
    cache = _load_city_cache()
    cache[norm(name)] = slug
    try:
        CITY_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CITY_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось сохранить слаг города: %s", e)


def resolve_city(city: str | None) -> tuple[str, str]:
    """Возвращает (слаг, источник): 'slug' | 'known' | 'cache' | 'translit'."""
    raw = (city or "").strip()
    if not raw:
        return "all", "known"
    key = re.sub(r"^(г|город)\s+", "", norm(raw))
    if key in CITY_SLUGS:
        return CITY_SLUGS[key], "known"
    if re.fullmatch(r"[a-z0-9_\-]+", raw):
        return raw, "slug"
    cached = _load_city_cache().get(key)
    if cached:
        return cached, "cache"
    return translit_city(key), "translit"


def city_matches(requested: str | None, shown: str | None) -> bool:
    """Совпадает ли город на странице (search-form/change-location) с запрошенным."""
    if not requested or not shown:
        return True
    a, b = norm(requested), norm(shown)
    a = re.sub(r"^(г|город)\s+", "", a)
    return a == b or a in b or b in a or stems(a) == stems(b)


# ---------------------------------------------------------------------------
# Категории
# ---------------------------------------------------------------------------
# Частые формулировки -> путь. Пути взяты из справочника (проверены на живом сайте).
ALIASES: list[tuple[str, str]] = [
    ("квартиры", "kvartiry/prodam-ASgBAgICAUSSA8YQ"),
    ("купить квартиру", "kvartiry/prodam-ASgBAgICAUSSA8YQ"),
    ("квартиры на продажу", "kvartiry/prodam-ASgBAgICAUSSA8YQ"),
    ("продажа квартир", "kvartiry/prodam-ASgBAgICAUSSA8YQ"),
    ("снять квартиру", "kvartiry/sdam/na_dlitelnyy_srok-ASgBAgICAkSSA8gQ8AeQUg"),
    ("аренда квартир", "kvartiry/sdam/na_dlitelnyy_srok-ASgBAgICAkSSA8gQ8AeQUg"),
    ("квартиры в аренду", "kvartiry/sdam/na_dlitelnyy_srok-ASgBAgICAkSSA8gQ8AeQUg"),
    ("квартиры посуточно", "kvartiry/sdam/posutochno/-ASgBAgICAkSSA8gQ8AeSUg"),
    ("телефоны", "telefony/mobile-ASgBAgICAUSwwQ2I_Dc"),
    ("смартфоны", "telefony/mobile-ASgBAgICAUSwwQ2I_Dc"),
    ("мобильные телефоны", "telefony/mobile-ASgBAgICAUSwwQ2I_Dc"),
    ("айфоны", "telefony/mobile-ASgBAgICAUSwwQ2I_Dc"),
    ("услуги", "predlozheniya_uslug"),
    ("ремонт техники", "predlozheniya_uslug/remont_i_obsluzhivanie_tehniki-ASgBAgICAUSYC7T3AQ"),
    ("ремонт бытовой техники", "predlozheniya_uslug/remont_i_obsluzhivanie_tehniki-ASgBAgICAUSYC7T3AQ"),
]

_categories: list[dict[str, Any]] | None = None


def categories() -> list[dict[str, Any]]:
    global _categories
    if _categories is None:
        try:
            _categories = json.loads(CATEGORIES_FILE.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("Справочник категорий не загружен: %s", e)
            _categories = []
        by_path = {c["path"]: c for c in _categories}
        for alias, path in ALIASES:
            base = by_path.get(path, {"name": alias, "parent": None})
            _categories.append({"name": alias, "parent": base.get("parent"), "path": path,
                                "alias_of": base.get("name")})
    return _categories


def _score(q: list[str], cat: dict[str, Any]) -> float | None:
    name = stems(cat["name"])
    parent = stems(cat.get("parent"))
    if not name:
        return None
    in_name = sum(1 for w in q if w in name)
    in_parent = sum(1 for w in q if w in parent and w not in name)
    if not in_name:
        return None  # ни одно слово запроса не совпало с названием
    score = 3.0 * in_name + 1.0 * in_parent - 0.7 * (len(name) - in_name) - 1.5 * (len(q) - in_name - in_parent)
    if norm(cat["name"]) == " ".join(norm(w) for w in q) or name == q:
        score += 4.0
    if cat.get("alias_of"):
        score += 1.0
    return score


def match_category(text: str, limit: int = 5) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Возвращает (уверенное совпадение или None, список кандидатов)."""
    raw = (text or "").strip()
    cats = categories()
    # Передали путь или ссылку: /kazan/telefony/mobile-…, telefony/mobile-…
    if "/" in raw or re.fullmatch(r"[a-z0-9_\-]+", raw):
        parts = [x for x in re.sub(r"^https?://[^/]+", "", raw).split("?")[0].split("/") if x]
        tops = {c["path"].split("/")[0] for c in cats}
        if len(parts) > 1 and parts[0] not in tops:
            parts = parts[1:]  # первым сегментом был город
        path = "/".join(parts)
        for c in cats:
            if c["path"] == path or c["path"].startswith((path + "-", path + "/-")):
                return c, [c]
        return {"name": raw, "parent": None, "path": path}, []
    q = stems(raw)
    if not q:
        return None, []
    scored = sorted(((s, c) for c in cats if (s := _score(q, c)) is not None), key=lambda x: -x[0])
    seen, cands = set(), []
    for s, c in scored:
        if c["path"] in seen:
            continue
        seen.add(c["path"])
        cands.append({**c, "score": round(s, 1)})
        if len(cands) >= limit:
            break
    best = cands[0] if cands else None
    if best:
        name = stems(best["name"]) + stems(best.get("parent"))
        all_found = all(w in name for w in q)
        clear_winner = len(cands) == 1 or best["score"] - cands[1]["score"] >= 1.0
        if all_found and (clear_winner or best.get("alias_of") or norm(best["name"]) == norm(raw)):
            return best, cands
    return None, cands


def path_slug(url: str) -> str:
    """'/kazan/predlozheniya_uslug/remont…-ASgB…?q=' -> 'predlozheniya_uslug/remont…' (без города и хвоста)."""
    path = re.sub(r"^https?://[^/]+", "", url or "").split("?")[0]
    parts = [x for x in path.split("/") if x][1:]
    return re.sub(r"/?-?ASgB[^/]*$", "", "/".join(parts)).rstrip("/")


def category_label(c: dict[str, Any]) -> str:
    name = c.get("alias_of") or c["name"]
    return f"{c['parent']} › {name}" if c.get("parent") else name


# ---------------------------------------------------------------------------
# Ссылка на выдачу
# ---------------------------------------------------------------------------
SORTS = {  # ключи — в виде norm(): подчёркивания и регистр не важны
    "default": None, "по умолчанию": None,
    "price asc": "1", "дешевле": "1", "сначала дешевле": "1", "цена по возрастанию": "1",
    "price desc": "2", "дороже": "2", "сначала дороже": "2", "цена по убыванию": "2",
    "date": "104", "новые": "104", "по дате": "104", "сначала новые": "104",
}


def sort_param(sort: str | None) -> tuple[str | None, str | None]:
    """Возвращает (значение параметра s, текст ошибки)."""
    if not sort:
        return None, None
    key = norm(sort.replace("_", " "))
    if key in SORTS:
        return SORTS[key], None
    return None, "Неизвестная сортировка. Допустимо: default, price_asc, price_desc, date."


def build_url(city_slug: str, path: str | None, query: str | None = None, price_min: int | None = None,
              price_max: int | None = None, sort: str | None = None) -> str:
    url = f"{BASE}/{city_slug}" + (f"/{path.strip('/')}" if path else "")
    params: list[tuple[str, str]] = []
    if query:
        params.append(("q", query.strip()))
    if price_min:
        params.append(("pmin", str(int(price_min))))
    if price_max:
        params.append(("pmax", str(int(price_max))))
    if sort:
        params.append(("s", sort))
    return url + (("?" + urlencode(params)) if params else "")
