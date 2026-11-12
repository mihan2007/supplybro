@echo off
chcp 65001 >nul
setlocal
rem Подключить учётную запись Dropbox к серверу (0.24.0). Ссылку из окна открыть в браузере
rem (приватное окно), войти в учётную запись Dropbox сервера и нажать «Разрешить».
set "DISTRO=Ubuntu-24.04"
title SupplyBro - подключить Dropbox
wsl -d %DISTRO% --cd "%~dp0." -- bash server/server.sh dropbox-connect
echo.
echo  Нажмите любую клавишу, чтобы закрыть окно.
pause >nul
