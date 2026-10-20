"""
Браузер для avito-mcp: одно видимое окно Edge со своим профилем, открывается лениво — при первом
вызове инструмента. Паузы между загрузками, ожидание отрисовки выдачи, распознавание проверок Авито.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

log = logging.getLogger("avito-mcp")

# ---------------------------------------------------------------------------
# Настройки (переменные окружения из блока "env" конфига Claude Desktop, см. README)
# ---------------------------------------------------------------------------
BROWSER_CHANNEL = os.environ.get("AVITO_MCP_BROWSER", "msedge").strip().lower()  # msedge | chrome | chromium
PROFILE_DIR = Path(
    os.environ.get("AVITO_MCP_PROFILE_DIR")
    or os.environ.get("AVITO_MCP_PROFILE")  # старое имя переменной из первой версии
    or Path.home() / ".avito-mcp" / "browser-profile"
).expanduser().resolve()
DELAY_SECONDS = max(3.0, float(os.environ.get("AVITO_MCP_DELAY", "5")))  # пауза между загрузками страниц
TIME_BUDGET = 45.0  # секунд на один вызов инструмента
PAGE_COST_ESTIMATE = DELAY_SECONDS + 15.0  # сколько примерно занимает одна загрузка страницы
CONTENT_TIMEOUT = 15.0  # сколько ждать отрисовки выдачи/карточки после загрузки
BLOCK_HOLD = 60.0  # столько секунд после загрузки не перезагружаем страницу с проверкой (капча сбросится)
ALLOWED_HOSTS = {"avito.ru", "www.avito.ru", "m.avito.ru"}
BASE = "https://www.avito.ru"

BLOCKED_MESSAGE = (
    "Авито показало проверку «я не робот» или ограничило доступ. Попросите пользователя пройти "
    "проверку в открытом окне Edge, дождитесь его подтверждения и повторите вызов с теми же "
    "параметрами. Не пытайтесь обойти проверку."
)
# Проверка бывает отдельной страницей («Доступ ограничен: проблема с IP» + капча), а бывает слоем
# поверх обычной выдачи (portals-container > [role=region]). Поэтому смотрим весь текст страницы.
BLOCK_MARKERS = ("доступ ограничен", "проблема с ip", "я не робот", "подтвердите, что вы не робот")
NOT_FOUND_MARKERS = ("такой страницы не существует", "такой страницы нет", "не нашли такую страницу")

JS_BLOCK_TEXT = r"""
() => {
  const parts = [document.title || ''];
  for (const e of document.querySelectorAll('[role=region], [role=dialog], [aria-modal=true]')) {
    const r = e.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) parts.push(e.innerText || '');
  }
  const h1 = document.querySelector('h1');
  if (h1) parts.push(h1.innerText || '');
  // если выдачи и карточки нет — смотрим и основной текст страницы
  if (!document.querySelector('[data-marker="item"], [data-marker^="item-view"]'))
    parts.push(document.body ? document.body.innerText.slice(0, 5000) : '');
  return parts.join('\n').toLowerCase();
}
"""

JS_CONTENT_READY = """() => !!document.querySelector(
  '[data-marker="catalog-serp"], [data-marker="item"], [data-marker^="item-view"], ' +
  '[data-marker="search-filters"], [data-marker="category-widget/sliced-link"], h1')"""


# ---------------------------------------------------------------------------
# Ссылки
# ---------------------------------------------------------------------------
def normalize_avito_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("/"):
        url = BASE + url
    if url and not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    p = urlparse(url)
    if (p.hostname or "").lower() not in ALLOWED_HOSTS:
        raise ValueError("Инструмент работает только со ссылками на avito.ru.")
    # мобильная версия -> полная, под неё написан разбор страниц
    return urlunparse(("https", "www.avito.ru", p.path or "/", "", p.query, ""))


def clean_search_url(url: str) -> str:
    """Убирает служебные параметры (context, localPriority и т. п.), оставляя фильтры и поиск."""
    p = urlparse(url)
    drop = {"context", "localpriority", "cd", "from", "radius_src"}
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in drop]
    return urlunparse(p._replace(query=urlencode(query), fragment=""))


def add_query(url: str, **params: str) -> str:
    """Добавляет или заменяет параметры запроса в ссылке."""
    p = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k not in params]
    query += [(k, str(v)) for k, v in params.items()]
    return urlunparse(p._replace(query=urlencode(query)))


def strip_url(u: str | None) -> str | None:
    """Ссылка на объявление без параметров."""
    if not u:
        return None
    p = urlparse(u)
    return urlunparse((p.scheme or "https", p.netloc, p.path, "", "", ""))


def page_from_url(url: str) -> int:
    value = dict(parse_qsl(urlparse(url).query)).get("p", "1")
    return int(value) if value.isdigit() and int(value) > 0 else 1


def with_page(url: str, n: int) -> str:
    p = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != "p"]
    if n > 1:
        query.append(("p", str(n)))
    return urlunparse(p._replace(query=urlencode(query)))


# ---------------------------------------------------------------------------
# Браузер
# ---------------------------------------------------------------------------
class Browser:
    """Одно окно браузера на всё время работы сервера. Открывается при первом вызове инструмента."""

    def __init__(self) -> None:
        self._pw = None
        self._ctx = None
        self._page = None
        self._last_navigation = 0.0
        self.lock = asyncio.Lock()

    def _on_close(self, *_: Any) -> None:
        log.info("Окно браузера закрыто; при следующем вызове откроется заново.")
        self._ctx = None
        self._page = None

    async def page(self):
        if self._ctx is None:
            if self._pw is None:
                self._pw = await async_playwright().start()
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            options: dict[str, Any] = {
                "headless": False,
                "locale": "ru-RU",
                "viewport": {"width": 1366, "height": 900},
                "chromium_sandbox": True,  # без этого Playwright добавляет --no-sandbox
            }
            if BROWSER_CHANNEL != "chromium":
                options["channel"] = BROWSER_CHANNEL
            log.info("Открываю браузер (%s), профиль: %s", BROWSER_CHANNEL, PROFILE_DIR)
            self._ctx = await self._pw.chromium.launch_persistent_context(str(PROFILE_DIR), **options)
            self._ctx.on("close", self._on_close)
            self._page = None
        if self._page is None or self._page.is_closed():
            open_pages = [p for p in self._ctx.pages if not p.is_closed()]
            self._page = open_pages[0] if open_pages else await self._ctx.new_page()
        return self._page

    async def pace(self) -> None:
        """Выдерживает паузу между загрузками (и перед действиями, которые грузят новую выдачу)."""
        wait = DELAY_SECONDS - (time.monotonic() - self._last_navigation)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_navigation = time.monotonic()

    def mark_navigation(self) -> None:
        self._last_navigation = time.monotonic()

    async def goto(self, url: str):
        page = await self.page()
        if time.monotonic() - self._last_navigation < BLOCK_HOLD and await is_blocked(page):
            # Пользователь, возможно, ещё решает капчу: перезагрузка её сбросит. Оставляем страницу как есть,
            # вызывающий код увидит проверку и вернёт status=blocked.
            log.info("На странице проверка Авито — не перезагружаю её, жду, пока пользователь пройдёт")
            return page
        await self.pace()
        log.info("Открываю %s", url)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        except PlaywrightTimeoutError:
            log.warning("Страница грузится долго, читаю то, что успело загрузиться")
        await self.settle(page)
        return page

    async def settle(self, page) -> None:
        """Ждёт, пока клиентский код Авито дорисует выдачу или карточку."""
        try:
            await page.wait_for_load_state("load", timeout=8000)
        except PlaywrightTimeoutError:
            pass
        started = time.monotonic()
        while time.monotonic() - started < CONTENT_TIMEOUT:
            try:
                if await page.evaluate(JS_CONTENT_READY):
                    break
            except Exception:  # noqa: BLE001 — страница могла перейти по редиректу
                pass
            await asyncio.sleep(0.5)
        await asyncio.sleep(1.0)

    async def close(self) -> None:
        try:
            if self._ctx is not None:
                await self._ctx.close()
        except Exception:  # noqa: BLE001 — при выходе любые ошибки закрытия не важны
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._ctx = self._page = self._pw = None


browser = Browser()


async def is_blocked(page) -> bool:
    try:
        text = await page.evaluate(JS_BLOCK_TEXT)
    except Exception:  # noqa: BLE001
        return False
    return any(m in text for m in BLOCK_MARKERS)


async def is_not_found(page) -> bool:
    try:
        h1 = (await page.evaluate("() => (document.querySelector('h1') || {}).innerText || ''")).lower()
    except Exception:  # noqa: BLE001
        return False
    return any(m in h1 for m in NOT_FOUND_MARKERS)


async def show_window(page) -> None:
    try:
        await page.bring_to_front()
    except Exception:  # noqa: BLE001
        pass


async def visible_text(page, limit: int) -> str:
    try:
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:  # noqa: BLE001
        return ""
    return re.sub(r"\n{3,}", "\n\n", text or "")[:limit]


def blocked_result(page_url: str | None = None, **extra: Any) -> dict[str, Any]:
    res: dict[str, Any] = {"status": "blocked", "message": BLOCKED_MESSAGE}
    if page_url:
        res["url"] = page_url
    res.update({k: v for k, v in extra.items() if v not in (None, "", [], {})})
    return res
