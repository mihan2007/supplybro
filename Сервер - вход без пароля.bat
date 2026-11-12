@echo off
chcp 65001 >nul
setlocal
rem Вход на сервер без пароля (0.24.0, один раз): ключ этого компьютера отправляется на сервер,
rem пароль root спросят последний раз. Дальше остальные «Сервер - ….bat» не спрашивают пароль.
set "DISTRO=Ubuntu-24.04"
title SupplyBro - вход на сервер без пароля
wsl -d %DISTRO% --cd "%~dp0." -- bash server/server.sh key
echo.
echo  Нажмите любую клавишу, чтобы закрыть окно.
pause >nul
