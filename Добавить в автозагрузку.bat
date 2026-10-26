@echo off
chcp 65001 >nul
setlocal
rem ============================================================
rem  Добавить сайт SupplyBro в автозагрузку Windows (запустить один раз).
rem  Кладёт в папку автозагрузки маленький файл, который при входе в Windows
rem  запускает «Запустить SupplyBro.bat» свёрнутым, без вопросов и без браузера.
rem  Убрать - «Убрать из автозагрузки.bat».
rem ============================================================
title SupplyBro - автозагрузка
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "TARGET=%STARTUP%\SupplyBro - автозапуск.bat"

> "%TARGET%" echo @echo off
>>"%TARGET%" echo chcp 65001 ^>nul
>>"%TARGET%" echo rem Автозапуск сайта SupplyBro при входе в Windows. Убрать - «Убрать из автозагрузки.bat» в папке сайта.
>>"%TARGET%" echo start "SupplyBro" /min "%~dp0Запустить SupplyBro.bat" /autostart

if not exist "%TARGET%" goto failed
echo.
echo  Готово: сайт SupplyBro будет запускаться сам при входе в Windows.
echo  Файл автозапуска: %TARGET%
echo.
echo  Нажмите любую клавишу, чтобы закрыть окно.
pause >nul
exit /b 0

:failed
echo.
echo  [!] Не удалось записать файл в папку автозагрузки:
echo      %STARTUP%
echo.
echo  Нажмите любую клавишу, чтобы закрыть окно.
pause >nul
exit /b 1
