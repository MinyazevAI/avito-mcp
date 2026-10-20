"""
Разбор страниц Авито на сохранённых фикстурах (tests/fixtures), без сети.
JS-разборщики выполняются в headless Edge на странице, загруженной через set_content.
  .venv/Scripts/python.exe -m pytest tests -q
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import catalog  # noqa: E402
import filters as F  # noqa: E402
import parsing  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture(scope="session")
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="msedge", headless=True)
        except Exception:  # noqa: BLE001 — нет Edge: пробуем Chromium из Playwright
            browser = pw.chromium.launch(headless=True)
        pg = browser.new_page()
        pg.route("**/*", lambda route: route.abort())  # никаких запросов в сеть
        yield pg
        browser.close()


def load(page, name: str):
    page.set_content((FIXTURES / f"{name}.html").read_text(encoding="utf-8"), wait_until="domcontentloaded")
    return page


# ---------------------------------------------------------------------------
# Выдача
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,count,crumb", [
    ("flats_serp", 24576, "Квартиры"),
    ("phones_serp", 24477, "Мобильные телефоны"),
    ("services_serp", 1850, "Стиральные, сушильные машины"),
])
def test_serp_items_and_summary(page, name, count, crumb):
    load(page, name)
    items = [parsing.search_item(r) for r in page.evaluate(parsing.JS_SEARCH)]
    assert len(items) >= 4
    for it in items:
        assert it["title"]
        assert it["url"].startswith("https://www.avito.ru/kazan/")
        assert "?" not in it["url"]
        assert isinstance(it.get("price_rub"), int)
    summary = parsing.page_summary(page.evaluate(parsing.JS_PAGE_INFO))
    assert summary["count"] == count
    assert summary["city"] == "Казань"
    assert crumb in summary["category_path"]


def test_serp_flats_title_parsing(page):
    load(page, "flats_serp")
    first = parsing.search_item(page.evaluate(parsing.JS_SEARCH)[0])
    assert first["rooms"] == 2 and first["area_m2"] == 46.8
    assert first["floor"] == 3 and first["floors_total"] == 17


def test_serp_next_page(page):
    load(page, "flats_serp")
    assert page.evaluate(parsing.JS_HAS_NEXT_PAGE) is True


def test_services_query_in_search_box(page):
    load(page, "services_serp")
    assert parsing.page_summary(page.evaluate(parsing.JS_PAGE_INFO))["query"] == "ремонт стиральных машин"


def test_landing_tiles(page):
    load(page, "services_landing")
    info = page.evaluate(parsing.JS_PAGE_INFO)
    assert not info["has_filters"]
    here = "predlozheniya_uslug/remont_i_obsluzhivanie_tehniki"
    direct = [t["name"] for t in info["tiles"] if catalog.path_slug(parsing.absolute(t["url"])).rsplit("/", 1)[0] == here]
    assert "Стиральные машины" in direct and "Холодильники" in direct
    assert "Диагностика" not in direct  # вложенные плитки не считаем подкатегориями


# ---------------------------------------------------------------------------
# Карточка
# ---------------------------------------------------------------------------
def test_card_flat(page):
    load(page, "flats_card")
    d = parsing.listing_details(page.evaluate(parsing.JS_LISTING), "https://www.avito.ru/x")
    assert d["title"].startswith("2-к. квартира")
    assert d["price_rub"] == 8840520
    assert d["address"] == "с. Усады, стр. 1, корп. 3"
    assert d["coords"] == [55.68745, 49.21568]
    assert d["floor"] == 3 and d["floors_total"] == 17
    assert d["house_type"] == "панельный"
    assert d["params"]["Общая площадь"] == "46.8 м²"


def test_card_phone_price_not_from_description(page):
    load(page, "phones_card")
    d = parsing.listing_details(page.evaluate(parsing.JS_LISTING), "https://www.avito.ru/x")
    assert d["price_rub"] == 32400  # в описании есть «30 000 ₽» — это не цена
    assert d["params"]["Модель"] == "iPhone 14 Pro"


def test_card_service(page):
    load(page, "services_card")
    d = parsing.listing_details(page.evaluate(parsing.JS_LISTING), "https://www.avito.ru/x")
    assert "Казань" in d["address"]
    assert d["price_list"] and any("Диагностика" in x for x in d["price_list"])
    assert "Производители" in d["params"]


# ---------------------------------------------------------------------------
# Панель фильтров
# ---------------------------------------------------------------------------
def groups_of(page, name):
    load(page, name)
    return page.evaluate(F.JS_EXTRACT_FILTERS, F.FILTERS_ROOT)


def by_name(groups, name):
    return next(g for g in groups if F.group_name(g) == name)


def test_filters_flats(page):
    groups = groups_of(page, "flats_filters")
    rooms = by_name(groups, "Количество комнат")
    assert rooms["type"] == "checkboxes" and rooms["options"][:2] == ["Студия", "1 комната"]
    assert rooms["has_more"] and rooms["_m"]["opts"][0] == "params[549]/checkbox/5695"
    year = by_name(groups, "Год постройки дома")
    assert year["type"] == "range" and year["_m"]["from"] == "params[164901]-from/input"
    assert by_name(groups, "Цена, ₽")["_m"]["to"] == "price-to/input"
    assert by_name(groups, "Где искать")["type"] == "location"
    assert by_name(groups, "Тип дома")["options"][0] == "Кирпичный"
    assert by_name(groups, "Не первый")["type"] == "checkbox"


def test_filters_phones(page):
    groups = groups_of(page, "phones_filters")
    assert by_name(groups, "Производитель")["type"] == "multiselect"
    assert "Хорошее" in by_name(groups, "Состояние")["options"]
    assert by_name(groups, "С Авито Доставкой")["_m"]["opts"] == ["d/checkbox/1"]
    color = by_name(groups, "Цвет")  # флажки без data-marker
    assert color["type"] == "checkboxes" and "Белый" in color["options"]
    assert by_name(groups, "Продавцы")["type"] == "switch"


def test_filters_services(page):
    groups = groups_of(page, "services_filters")
    assert by_name(groups, "Техника")["type"] == "list"
    assert "Bosch" in by_name(groups, "Производители")["options"]
    assert not any(F.group_name(g).startswith("Цена") for g in groups)  # у услуг нет фильтра цены


def test_public_group_is_compact(page):
    groups = groups_of(page, "flats_filters")
    pub = [F.public_group(g) for g in groups]
    assert all("_m" not in g and "values" not in g for g in pub)
    assert F.summarize(groups)[0] == "Категория — список"


# ---------------------------------------------------------------------------
# Нечёткое сопоставление и справочники (без браузера)
# ---------------------------------------------------------------------------
ROOMS = ["Студия", "1 комната", "2 комнаты", "3 комнаты", "4 комнаты"]


@pytest.mark.parametrize("value,expected", [("студия", 0), ("1", 1), ("2к", 2), ("3 комнаты", 3), ("4+", 4), ("5", None)])
def test_fuzzy_rooms(value, expected):
    assert F.fuzzy_index(value, ROOMS) == expected


@pytest.mark.parametrize("name,expected", [
    ("комнаты", "Количество комнат"), ("Площадь", "Общая площадь"), ("год постройки", "Год постройки дома"),
    ("авито доставка", "С Авито Доставкой"), ("цена", "Цена, ₽"), ("кухня", "Площадь кухни"), ("Ёмкость", None),
])
def test_find_group(name, expected):
    names = ["Количество комнат", "Цена, ₽", "Общая площадь", "Год постройки дома", "С Авито Доставкой", "Площадь кухни"]
    groups = [{"name": n, "key": "price" if n.startswith("Цена") else n,
               "type": "checkbox" if "Доставк" in n else "x"} for n in names]
    g = F.find_group(groups, name)
    assert (g and g["name"]) == expected


def test_used_condition_and_numbers():
    st = ["Новое", "Отличное", "Хорошее", "Удовлетворительное", "Требуется ремонт"]
    assert F.expand_values({"name": "Состояние", "options": st}, ["б/у"]) == ["Отличное", "Хорошее", "Удовлетворительное"]
    assert F.fuzzy_index("новый", st) == 0
    assert F.parse_number("12 млн") == 12_000_000 and F.parse_number("30 тыс") == 30_000
    assert F.parse_number("2,7") == 2.7 and F.parse_number("30 000") == 30_000
    assert F.same_number("12 000 000", 12_000_000)


@pytest.mark.parametrize("text,path_start", [
    ("квартиры", "kvartiry/prodam"), ("снять квартиру", "kvartiry/sdam/na_dlitelnyy_srok"),
    ("мобильные телефоны", "telefony/mobile"), ("стиральные машины", "predlozheniya_uslug/remont_i_obsluzhivanie_tehniki/stiralnye"),
    ("/kazan/telefony/mobile-ASgBAgICAUSwwQ2I_Dc", "telefony/mobile-ASgB"), ("telefony/mobile", "telefony/mobile-ASgB"),
])
def test_match_category(text, path_start):
    best, _ = catalog.match_category(text)
    assert best and best["path"].startswith(path_start)


def test_unknown_category_gives_candidates():
    best, cands = catalog.match_category("зимние шины R17")
    assert best is None and any("Шины" in c["name"] for c in cands)


@pytest.mark.parametrize("city,slug", [
    ("Казань", "kazan"), ("г. Казань", "kazan"), ("Набережные Челны", "naberezhnye_chelny"),
    ("Нижний Новгород", "nizhniy_novgorod"), ("spb", "sankt-peterburg"), ("Ульяновск", "ulyanovsk"),
    ("Россия", "all"), ("kazan", "kazan"),
])
def test_resolve_city(city, slug, tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "CITY_CACHE_FILE", tmp_path / "cities.json")
    assert catalog.resolve_city(city)[0] == slug


def test_build_url_and_sort():
    assert catalog.sort_param("price_asc") == ("1", None)
    assert catalog.sort_param("Дороже") == ("2", None)
    assert catalog.sort_param("date") == ("104", None)
    assert catalog.sort_param("что-то")[1]
    url = catalog.build_url("kazan", "telefony/mobile-ASgBAgICAUSwwQ2I_Dc", "iphone 13", None, 30000, "1")
    assert url == "https://www.avito.ru/kazan/telefony/mobile-ASgBAgICAUSwwQ2I_Dc?q=iphone+13&pmax=30000&s=1"


def test_clean_search_url():
    from browser import add_query, clean_search_url

    u = clean_search_url("https://www.avito.ru/kazan/x?context=abc&f=ASg&localPriority=0&q=a")
    assert u == "https://www.avito.ru/kazan/x?f=ASg&q=a"
    assert add_query(u, s="1").endswith("&s=1")


# ---------------------------------------------------------------------------
# Фикстуры не содержат данных продавцов
# ---------------------------------------------------------------------------
PHONE_RE = re.compile(r"(?:\+7|\b8)[\s\-(]*9\d{2}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.html")), ids=lambda p: p.name)
def test_fixtures_have_no_seller_data(path):
    html = path.read_text(encoding="utf-8")
    assert not PHONE_RE.search(html)
    assert 'data-marker="seller-info' not in html
    assert 'data-marker="item-phone-button' not in html
