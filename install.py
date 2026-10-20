#!/usr/bin/env python3
"""
Установщик avito-mcp для Claude Desktop (Windows).

Обычно запускается из install.bat. Можно и напрямую любым Python 3.10+:
    python install.py            — установить или обновить (безопасно запускать повторно)
    python install.py --yes      — то же без вопросов
    python install.py --uninstall
Что делает:
  1. создаёт .venv в папке проекта (если его нет) и перезапускается из него;
  2. ставит или обновляет зависимости из requirements.txt;
  3. проверяет, что установлен Microsoft Edge (браузер берётся установленный, `playwright install` не нужен);
  4. проверяет, что Claude Desktop закрыт (иначе он перезапишет конфиг при выходе);
  5. прописывает сервер в claude_desktop_config.json (обычная и MSIX-версия Claude Desktop):
     меняется только запись mcpServers.avito, остальное содержимое и форматирование сохраняются,
     файл пишется в UTF-8 без BOM, рядом кладётся резервная копия *.bak-ГГГГММДД-ЧЧММСС;
  6. запускает прописанную команду по stdio через клиент mcp и выводит список инструментов.
Ключи:
  --yes, -y        ничего не спрашивать; если Claude Desktop открыт — выйти с ошибкой
  --config PATH    писать в указанный файл вместо настоящего конфига Claude Desktop
  --uninstall      удалить запись avito из конфига (с резервной копией)
  --skip-deps      не ставить зависимости
  --force          записать конфиг, даже если Claude Desktop запущен
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"
VENV = HERE / ".venv"
VENV_PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
SERVER_NAME = "avito"
REEXEC_FLAG = "AVITO_MCP_INSTALL_IN_VENV"


def say(text: str = "") -> None:
    print(text, flush=True)


def interactive(args) -> bool:
    return not args.yes and sys.stdin is not None and sys.stdin.isatty()


# ---------------------------------------------------------------------------
# Окружение
# ---------------------------------------------------------------------------
def in_project_venv() -> bool:
    try:
        return Path(sys.prefix).resolve() == VENV.resolve()
    except OSError:
        return False


def venv_works() -> bool:
    if not VENV_PY.exists():
        return False
    try:
        r = subprocess.run([str(VENV_PY), "-c", "import sys; print(sys.version_info >= (3, 10))"],
                           capture_output=True, text=True, timeout=60)
        return r.returncode == 0 and r.stdout.strip() == "True"
    except Exception:  # noqa: BLE001
        return False


def ensure_venv_and_reexec() -> int | None:
    """Создаёт .venv при необходимости и перезапускает установщик из него. None — уже внутри .venv."""
    if in_project_venv() or os.environ.get(REEXEC_FLAG):
        return None
    if not venv_works():
        if VENV.exists():
            say("     Окружение .venv повреждено или создано другим Python — пересоздаю.")
            shutil.rmtree(VENV, ignore_errors=True)
        say(f"     Создаю окружение {VENV} ...")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
    else:
        say(f"     Окружение {VENV} уже есть.")
    env = {**os.environ, REEXEC_FLAG: "1", "PYTHONUTF8": "1"}
    return subprocess.call([str(VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]], env=env)


def install_requirements() -> None:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q",
                           "--upgrade", "pip"])
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q",
                           "-r", str(HERE / "requirements.txt")])


def edge_path() -> Path | None:
    if sys.platform != "win32":
        return None
    roots = [os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")]
    for r in roots:
        if r:
            p = Path(r, "Microsoft", "Edge", "Application", "msedge.exe")
            if p.exists():
                return p
    return None


# ---------------------------------------------------------------------------
# Claude Desktop
# ---------------------------------------------------------------------------
def claude_desktop_processes() -> list[str]:
    """Пути запущенных процессов приложения Claude Desktop (не Claude Code и не служб пакета)."""
    if sys.platform != "win32":
        return []
    ps = "Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\" | ForEach-Object { $_.ExecutablePath }"
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=60).stdout
    except Exception:  # noqa: BLE001
        return []
    paths = [p.strip() for p in out.splitlines() if p.strip()]
    # Claude Desktop: %LOCALAPPDATA%\AnthropicClaude\app-…\claude.exe или WindowsApps\Claude_…\app\claude.exe.
    # Claude Code лежит в другом месте (.local\bin, расширения VS Code и т. п.) — его не считаем.
    return [p for p in paths if re.search(r"AnthropicClaude|WindowsApps\\Claude_", p, re.I)]


def wait_desktop_closed(args) -> bool:
    running = claude_desktop_processes()
    if not running:
        say("     Claude Desktop закрыт.")
        return True
    if args.force:
        say("     Claude Desktop запущен, но указан --force — записываю.")
        return True
    say("     Claude Desktop запущен. Закройте его ПОЛНОСТЬЮ: значок Claude в трее (у часов) -> Выход.")
    if not interactive(args):
        say("     Запустите установку снова, когда Claude Desktop будет закрыт.")
        return False
    while running:
        try:
            input("     Когда закроете, нажмите Enter (или Ctrl+C для отмены)... ")
        except (EOFError, KeyboardInterrupt):
            say()
            return False
        running = claude_desktop_processes()
        if running:
            say("     Claude Desktop всё ещё запущен.")
    say("     Claude Desktop закрыт.")
    return True


def config_paths() -> list[Path]:
    """Конфиги Claude Desktop: существующие файлы; если их нет — где их создать.

    Обычная версия читает %APPDATA%\\Claude\\claude_desktop_config.json, версия из Microsoft Store (MSIX) —
    %LOCALAPPDATA%\\Packages\\Claude_…\\LocalCache\\Roaming\\Claude\\claude_desktop_config.json.
    """
    candidates: list[Path] = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            for package in sorted((Path(local) / "Packages").glob("Claude_*")):
                folder = package / "LocalCache" / "Roaming" / "Claude"
                if folder.is_dir():
                    candidates.append(folder / "claude_desktop_config.json")
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
    else:
        candidates.append(Path.home() / ".config" / "Claude" / "claude_desktop_config.json")
    existing = [p for p in candidates if p.exists()]
    return existing or candidates


# ---------------------------------------------------------------------------
# Запись конфига
# ---------------------------------------------------------------------------
def server_entry() -> dict:
    return {"command": str(VENV_PY), "args": [str(SERVER)], "env": {"PYTHONUTF8": "1"}}


def _format_like(original: str, data: dict) -> str:
    """JSON в стиле исходного файла: отступ, переводы строк, перевод строки в конце."""
    m = re.search(r"\n([ \t]+)\S", original)
    indent: int | str = 2
    if m:
        indent = "\t" if m.group(1).startswith("\t") else len(m.group(1))
    text = json.dumps(data, ensure_ascii=False, indent=indent)
    if original.endswith("\n"):
        text += "\n"
    if "\r\n" in original:
        text = text.replace("\n", "\r\n")
    return text


def _read_config(path: Path) -> tuple[dict | None, str]:
    if not path.exists():
        return {}, ""
    original = path.read_bytes().decode("utf-8-sig")
    if not original.strip():
        return {}, original
    try:
        data = json.loads(original)
    except json.JSONDecodeError as e:
        say(f"     ! В файле {path} ошибка JSON ({e}). Не трогаю его — поправьте вручную (см. README).")
        return None, original
    if not isinstance(data, dict):
        say(f"     ! Непонятный формат {path}. Не трогаю его.")
        return None, original
    return data, original


def _write_config(path: Path, original: str, data: dict, before: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # UTF-8 без BOM; newline="" — чтобы Windows не заменила LF на CRLF
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(_format_like(original, data))
    # Проверка: кроме mcpServers.avito ничего не изменилось
    after = json.loads(path.read_text(encoding="utf-8"))
    strip = lambda d: {**d, "mcpServers": {k: v for k, v in (d.get("mcpServers") or {}).items() if k != SERVER_NAME}}  # noqa: E731
    a, b = strip(after), strip(before)
    if not before.get("mcpServers") and not a["mcpServers"]:
        a.pop("mcpServers"), b.pop("mcpServers", None)
    assert a == b, "изменилось что-то кроме mcpServers.avito"
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf"), "в файле BOM"


def _backup(path: Path) -> Path:
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    return backup


def register(paths: list[Path]) -> list[Path]:
    entry = server_entry()
    written = []
    for path in paths:
        data, original = _read_config(path)
        if data is None:
            continue
        current = (data.get("mcpServers") or {}).get(SERVER_NAME) if isinstance(data.get("mcpServers"), dict) else None
        if current == entry:
            say(f"     Уже прописано, без изменений: {path}")
            written.append(path)
            continue
        before = json.loads(json.dumps(data))
        if path.exists():
            say(f"     Резервная копия: {_backup(path)}")
        if not isinstance(data.get("mcpServers"), dict):
            data["mcpServers"] = {}
        data["mcpServers"][SERVER_NAME] = entry
        _write_config(path, original, data, before)
        say(f"     {'Обновлено' if current else 'Прописано'}: {path}")
        written.append(path)
    return written


def unregister(paths: list[Path]) -> int:
    removed = 0
    for path in paths:
        if not path.exists():
            continue
        data, original = _read_config(path)
        if not data or SERVER_NAME not in (data.get("mcpServers") or {}):
            say(f"     Записи avito нет: {path}")
            continue
        before = json.loads(json.dumps(data))
        say(f"     Резервная копия: {_backup(path)}")
        del data["mcpServers"][SERVER_NAME]
        _write_config(path, original, data, before)
        say(f"     Запись avito удалена: {path}")
        removed += 1
    return removed


# ---------------------------------------------------------------------------
# Проверка установленной команды
# ---------------------------------------------------------------------------
async def _list_tools(entry: dict) -> list[str]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=entry["command"], args=entry["args"],
                                   env={**os.environ, **entry.get("env", {})})
    with open(os.devnull, "w") as devnull:  # логи сервера в stderr здесь не нужны
        async with stdio_client(params, errlog=devnull) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return [t.name for t in (await s.list_tools()).tools]


def verify(config: Path) -> bool:
    """Запускает проверку в новом процессе: только что установленные пакеты (pywin32 для mcp)
    подключаются при старте интерпретатора, в текущем процессе их может не быть видно."""
    r = subprocess.run([str(VENV_PY), str(Path(__file__).resolve()), "--verify-only", str(config)],
                       env={**os.environ, "PYTHONUTF8": "1"})
    return r.returncode == 0


def verify_here(config: Path) -> bool:
    entry = json.loads(config.read_text(encoding="utf-8"))["mcpServers"][SERVER_NAME]
    try:
        tools = asyncio.run(asyncio.wait_for(_list_tools(entry), timeout=90))
    except Exception as e:  # noqa: BLE001
        say(f"     ! Сервер не запустился: {type(e).__name__}: {e}")
        return False
    say(f"     Сервер запускается, инструменты ({len(tools)}): {', '.join(tools)}")
    return bool(tools)


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Установка avito-mcp в Claude Desktop")
    parser.add_argument("--yes", "-y", action="store_true", help="ничего не спрашивать")
    parser.add_argument("--config", type=Path, help="писать в этот файл вместо настоящего конфига Claude Desktop")
    parser.add_argument("--uninstall", action="store_true", help="удалить запись avito из конфига")
    parser.add_argument("--skip-deps", action="store_true", help="не ставить зависимости")
    parser.add_argument("--force", action="store_true", help="записать, даже если Claude Desktop запущен")
    parser.add_argument("--verify-only", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.verify_only:
        return 0 if verify_here(args.verify_only) else 1

    if sys.version_info < (3, 10):
        say("Нужен Python 3.10 или новее: https://www.python.org/downloads/")
        return 1
    paths = [args.config.expanduser().resolve()] if args.config else config_paths()

    if args.uninstall:
        say("Удаление avito-mcp из Claude Desktop")
        if not args.config and not wait_desktop_closed(args):
            return 2
        n = unregister(paths)
        say("Готово. Перезапустите Claude Desktop." if n else "Нечего удалять.")
        say(f"Папку проекта ({HERE}) и профиль браузера (%USERPROFILE%\\.avito-mcp) можно удалить вручную.")
        return 0

    if not os.environ.get(REEXEC_FLAG) and not in_project_venv():
        say("1/5  Окружение Python")
        code = ensure_venv_and_reexec()
        if code is not None:
            return code
    if not args.skip_deps:
        say("2/5  Устанавливаю зависимости (mcp, playwright)...")
        try:
            install_requirements()
        except subprocess.CalledProcessError:
            say("     ! Не удалось установить зависимости — посмотрите текст ошибки выше (нужен интернет).")
            return 1
        say("     Готово.")
    else:
        say("2/5  Зависимости не ставлю (--skip-deps).")

    say("3/5  Проверяю Microsoft Edge...")
    edge = edge_path()
    if edge:
        say(f"     Edge найден: {edge}")
    else:
        say("     ! Edge не найден. Установите его (https://www.microsoft.com/edge) или пропишите в конфиге")
        say('       "AVITO_MCP_BROWSER": "chrome" (см. README).')

    say("4/5  Прописываю сервер в настройки Claude Desktop...")
    if not args.config and not wait_desktop_closed(args):
        return 2
    written = register(paths)
    if not written:
        say("     ! Не удалось записать конфиг. Добавьте блок вручную (см. README):")
        say(json.dumps({"mcpServers": {SERVER_NAME: server_entry()}}, ensure_ascii=False, indent=2))
        return 1

    say("5/5  Проверяю установленную команду...")
    if not verify(written[0]):
        return 1
    say()
    say("Готово. Запустите Claude Desktop (если он был открыт — перезапустите через трей: значок Claude -> Выход).")
    say("Проверка: Настройки -> Разработчик, сервер avito должен быть запущен.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
