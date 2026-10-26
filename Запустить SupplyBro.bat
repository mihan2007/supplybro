@echo off
chcp 65001 >nul
setlocal
rem ============================================================
rem  Запуск сайта SupplyBro двойным щелчком.
rem  1) один раз настраивает Ubuntu (WSL), чтобы она не выключалась сама;
rem  2) проверяет, что программа-прокси Windows запущена;
rem  3) собирает и запускает сайт (run.sh внутри Ubuntu);
rem  4) ждёт, пока сайт ответит, и открывает его в браузере.
rem  С параметром /autostart (из автозагрузки Windows): без вопросов и без браузера,
rem  прокси ждёт до 2 минут - он может запуститься позже сайта.
rem ============================================================
set "DISTRO=Ubuntu-24.04"
set "PORT=8000"
set "PROXY_PORT=10809"
set "WSLCONFIG=%USERPROFILE%\.wslconfig"
set "AUTO=0"
if /i "%~1"=="/autostart" set "AUTO=1"
title SupplyBro - запуск
echo.
echo  SupplyBro: запуск сайта
echo  -----------------------
echo.

rem --- 1. Ubuntu не должна выключаться сама, когда окна закрыты (настраивается один раз) ---
findstr /i /c:"instanceIdleTimeout" "%WSLCONFIG%" >nul 2>&1
if not errorlevel 1 goto proxy
echo  Первая настройка: Ubuntu будет работать в фоне, пока компьютер включён.
echo  Ubuntu сейчас перезапустится - открытые окна Ubuntu закроются.
>>"%WSLCONFIG%" echo.
>>"%WSLCONFIG%" echo [general]
>>"%WSLCONFIG%" echo instanceIdleTimeout=-1
wsl --shutdown
echo  Готово.
echo.

:proxy
rem --- 2. Прокси Windows: через него сайт ходит в NocoDB и скачивает библиотеки ---
set /a PTRIES=0
:proxycheck
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %PROXY_PORT% -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if not errorlevel 1 goto start
if "%AUTO%"=="1" goto proxywait
echo  [!] Программа-прокси на 127.0.0.1:%PROXY_PORT% не запущена.
echo      Без неё сайт не получит данные из NocoDB.
echo      Включите её и нажмите любую клавишу, чтобы продолжить.
pause >nul
goto start

:proxywait
rem В автозагрузке ждём прокси до 2 минут, потом запускаем сайт всё равно.
set /a PTRIES+=1
if %PTRIES% gtr 24 goto proxygiveup
if %PTRIES%==1 echo  Жду программу-прокси на 127.0.0.1:%PROXY_PORT%...
timeout /t 5 /nobreak >nul
goto proxycheck

:proxygiveup
echo  [!] Прокси не запустился за 2 минуты - запускаю сайт без него.

:start
rem --- 3. Сборка и запуск сайта внутри Ubuntu ---
echo  Запускаю сайт: первый раз - несколько минут, дальше - секунды...
echo.
wsl -d %DISTRO% --cd "%~dp0." -- bash run.sh
if errorlevel 1 goto failed

rem --- 4. Ждём, пока сайт ответит, и открываем его в браузере ---
echo.
echo  Жду, пока сайт ответит...
set /a TRIES=0
:wait
curl.exe -s --noproxy "*" -o nul http://127.0.0.1:%PORT%/health
if not errorlevel 1 goto ready
set /a TRIES+=1
if %TRIES% geq 30 goto noanswer
timeout /t 1 /nobreak >nul
goto wait

:ready
if "%AUTO%"=="0" start "" http://127.0.0.1:%PORT%
echo  Сайт работает: http://127.0.0.1:%PORT%
echo  Окно закроется само через 5 секунд, сайт продолжит работать в фоне.
timeout /t 5 >nul
exit /b 0

:noanswer
echo.
echo  [!] Сайт запущен, но не отвечает. Последние строки журнала сайта:
wsl -d %DISTRO% --cd "%~dp0." -- docker compose logs --tail 30 web
goto end

:failed
echo.
echo  [!] Запуск не удался - текст ошибки выше.

:end
echo.
echo  Нажмите любую клавишу, чтобы закрыть окно.
pause >nul
exit /b 1
