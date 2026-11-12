@echo off
chcp 65001 >nul
setlocal
rem Отправить текущую версию сайта на сервер и перезапустить его там (0.24.0).
rem Куда - server\server.conf, как - server\server.sh deploy. Без входа по ключу пароль root сервера
rem спросят дважды - один раз настройте «Сервер - вход без пароля.bat».
set "DISTRO=Ubuntu-24.04"
title SupplyBro - отправить версию на сервер
wsl -d %DISTRO% --cd "%~dp0." -- bash server/server.sh deploy
echo.
echo  Нажмите любую клавишу, чтобы закрыть окно.
pause >nul
