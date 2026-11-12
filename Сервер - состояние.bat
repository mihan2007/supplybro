@echo off
chcp 65001 >nul
setlocal
rem Состояние сервера (0.24.0): версия сайта, папка конкурсов, контейнеры, Dropbox, место на диске;
rem с 0.25.0 - коммит версии и сверка настроек сервера с образцом сервера и с ноутбуком.
set "DISTRO=Ubuntu-24.04"
title SupplyBro - состояние сервера
wsl -d %DISTRO% --cd "%~dp0." -- bash server/server.sh status
echo.
echo  Нажмите любую клавишу, чтобы закрыть окно.
pause >nul
