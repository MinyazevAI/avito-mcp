@echo off
rem Установка avito-mcp для Claude Desktop. Двойной щелчок — установить или обновить.
rem Из командной строки: install.bat --yes | --uninstall | --config PATH (см. README).
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Пауза в конце нужна только при запуске двойным щелчком (окно иначе сразу закроется)
set "PAUSE_AT_END="
echo %cmdcmdline% | find /i "%~0" >nul && set "PAUSE_AT_END=1"
for %%A in (%*) do (
  if /i "%%~A"=="--yes" set "PAUSE_AT_END="
  if /i "%%~A"=="-y" set "PAUSE_AT_END="
)

set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY goto nopython

%PY% "%~dp0install.py" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
  echo.
  echo Установка не завершена ^(код %CODE%^). Прочитайте сообщения выше.
  echo Если непонятно, что делать, скопируйте текст и пришлите его в чат с Claude.
)
goto end

:nopython
set "CODE=1"
echo.
echo Не найден Python 3.10 или новее.
where py >nul 2>nul && (echo Найден Python, но старой версии:& py -3 --version)
echo Скачайте Python с https://www.python.org/downloads/ и при установке отметьте "Add python.exe to PATH".
echo Потом снова запустите install.bat.

:end
if defined PAUSE_AT_END (
  echo.
  pause
)
exit /b %CODE%
