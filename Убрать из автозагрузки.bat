@echo off
chcp 65001 >nul
setlocal
rem Убрать сайт SupplyBro из автозагрузки Windows. Сам сайт и его данные не трогаются.
title SupplyBro - автозагрузка
set "TARGET=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\SupplyBro - автозапуск.bat"
if exist "%TARGET%" del "%TARGET%"
echo.
if exist "%TARGET%" (
  echo  [!] Не удалось удалить файл автозапуска - удалите его вручную: Win+R, shell:startup
) else (
  echo  Готово: сайт SupplyBro больше не запускается сам при входе в Windows.
)
echo.
echo  Нажмите любую клавишу, чтобы закрыть окно.
pause >nul
