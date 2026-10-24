@echo off
rem Служебный режим: открыть браузер, когда приложение уже запустится.
rem Вызывается из блока [4/4] строкой: start "" /b cmd /c call "%~f0" --open-browser <порт>
if /i "%~1"=="--open-browser" (
    set "OPEN_PORT=%~2"
    if not defined OPEN_PORT set "OPEN_PORT=8501"
    goto open_browser_when_ready
)

chcp 65001 >nul
setlocal EnableExtensions
title ИИ-ассистент 1С — запуск

rem =====================================================================
rem  start_app.bat — запуск Streamlit-приложения "ИИ-ассистент для 1С".
rem
rem  Зачем нужен этот файл:
rem    * не требует PowerShell, поэтому не упирается в PSSecurityException
rem      (политику выполнения скриптов) — здесь обычный cmd/bat;
rem    * сам создаёт .venv, если его нет;
rem    * ставит зависимости ВНУТРЬ .venv, а не глобально;
rem    * запускает приложение и не закрывает окно при ошибке.
rem
rem  Запуск: дважды кликнуть по файлу в Проводнике (или из cmd:
rem  start_app.bat в папке проекта).
rem =====================================================================

rem --- 1. Переход в папку проекта (папка, где лежит сам .bat) -----------
cd /d "%~dp0"
if errorlevel 1 (
    echo [ОШИБКА] Не удалось перейти в папку проекта: %~dp0
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   ИИ-ассистент 1С — запуск приложения
echo   Папка проекта: %CD%
echo ============================================================
echo.

if not exist "requirements.txt" (
    echo [ОШИБКА] Файл requirements.txt не найден в папке %CD%
    echo Положите start_app.bat в корень проекта и запустите снова.
    pause
    exit /b 1
)

rem --- 2. Виртуальное окружение: создать, если его нет ------------------
rem Проверяем не только папку .venv, но и activate.bat внутри неё:
rem так мы отловим и отсутствующее, и "битое" окружение.
if exist ".venv\Scripts\activate.bat" goto venv_ready

echo [2/4] Виртуальное окружение .venv не найдено — создаю...

rem Ищем рабочий Python. Проверяем именно запуском, а не where:
rem заглушка Microsoft Store тоже лежит в PATH и находится через where.
set "PY="
python -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
    py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    echo [ОШИБКА] Рабочий Python не найден.
    echo Установите Python 3.12+ с python.org и включите
    echo "Add python.exe to PATH", затем запустите файл снова.
    pause
    exit /b 1
)

%PY% -m venv .venv
if errorlevel 1 (
    echo [ОШИБКА] Не удалось создать .venv командой "%PY% -m venv .venv".
    pause
    exit /b 1
)
echo [2/4] Готово: .venv создан.
echo.

:venv_ready

rem --- 3. Активация окружения и установка зависимостей -----------------
rem call + activate.bat работает в cmd/bat. Именно поэтому в PowerShell
rem вылезал PSSecurityException на Activate.ps1 — здесь его нет.
echo [3/4] Активирую .venv и проверяю зависимости...
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [ОШИБКА] Не удалось активировать .venv
    pause
    exit /b 1
)
if "{%VIRTUAL_ENV%}"=="{}" (
    echo [ОШИБКА] Окружение .venv не активировалось.
    echo Удалите папку .venv и запустите start_app.bat заново.
    pause
    exit /b 1
)

rem Обновление pip не критично: если не получилось — идём дальше.
python -m pip install --upgrade pip --disable-pip-version-check
if errorlevel 1 echo [ПРЕДУПРЕЖДЕНИЕ] Не удалось обновить pip, продолжаю.

python -m pip install -r requirements.txt --disable-pip-version-check
if errorlevel 1 (
    echo.
    echo [ОШИБКА] Не удалось установить зависимости из requirements.txt.
    echo Проверьте интернет-соединение и запустите start_app.bat снова.
    pause
    exit /b 1
)
echo [3/4] Зависимости в порядке.
echo.

rem --- 4. Запуск приложения ---------------------------------------------
rem Запускаем Streamlit в режиме headless:
rem  * он не пытается сам открыть браузер (это ненадёжно и у нас не сработало);
rem  * не спрашивает e-mail при первом запуске, а значит не создаёт папку
rem    %USERPROFILE%\.streamlit (из-за неё был "[WinError 5] Отказано в доступе");
rem  * --browser.gatherUsageStats=false выключает телеметрию.
rem Браузер откроем сами, когда сервер начнёт отвечать (блок в конце файла).
rem "python -m streamlit run app.py" — то же самое, что "streamlit run app.py",
rem но надёжнее: не зависит от того, что оказалось в PATH первым.
rem
rem Важно: если 8501 занят (например, Docker-контейнером или другим
rem Streamlit), берём следующий свободный порт и открываем браузер именно
rem на нём. Иначе bat открывал бы 8501 и показывал чужое приложение.
set "PORT=8501"

:find_free_port
netstat -ano | findstr ":%PORT% " | findstr "LISTENING" >nul 2>nul
if errorlevel 1 goto port_selected
set /a PORT+=1
if %PORT% GTR 8510 goto no_free_port
goto find_free_port

:no_free_port
echo [ОШИБКА] Не найдено свободных портов в диапазоне 8501-8510.
echo Остановите Docker-контейнер или другое приложение Streamlit и запустите снова.
pause
exit /b 1

:port_selected
echo [4/4] Запускаю приложение на порту %PORT%...
echo   Адрес:  http://localhost:%PORT%
echo   Браузер откроется сам, как только приложение загрузится.
echo   Если окно браузера не появилось - откройте адрес вручную.
echo   Стоп:   Ctrl+C в этом окне
echo.

rem Запускаем служебный блок открытия браузера в фоне (он дождётся сервера).
rem Важно: именно "cmd /c call", иначе start оставляет лишний cmd с приглашением.
start "" /b cmd /c call "%~f0" --open-browser %PORT%
python -m streamlit run app.py --server.headless=true --server.port=%PORT% --browser.gatherUsageStats=false

if errorlevel 1 (
    echo.
    echo [ВНИМАНИЕ] Streamlit завершился с ошибкой.
    echo Если вы сами остановили приложение через Ctrl+C — это нормально,
    echo иначе посмотрите текст ошибки выше.
) else (
    echo.
    echo Приложение остановлено.
)

echo.
echo Окно закроется после нажатия любой клавиши.
pause
endlocal
exit /b

rem =====================================================================
rem  Служебный блок: дождаться, пока приложение начнёт отвечать,
rem  и только потом открыть браузер (иначе страница откроется
rem  раньше сервера и покажет "не удаётся подключиться").
rem =====================================================================
:open_browser_when_ready
rem Если curl недоступен (очень старые версии Windows) — просто подождём.
if not exist "%SystemRoot%\System32\curl.exe" (
    ping -n 7 127.0.0.1 >nul 2>nul
    goto open_browser_now
)
rem До 60 секунд проверяем, отвечает ли сервер.
for /l %%i in (1,1,60) do (
    "%SystemRoot%\System32\curl.exe" -s -o nul --max-time 2 "http://localhost:%OPEN_PORT%"
    if not errorlevel 1 goto open_browser_now
    ping -n 2 127.0.0.1 >nul 2>nul
)

:open_browser_now
echo.
echo   Открываю браузер: http://localhost:%OPEN_PORT%
start "" "http://localhost:%OPEN_PORT%"
exit /b
