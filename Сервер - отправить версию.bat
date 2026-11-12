@echo off
chcp 65001 >nul
setlocal
rem Отправить на сервер версию из git (последний коммит, 0.25.0) и перезапустить там сайт. Если есть изменения
rem без коммита или коммиты без push - не отправляет и говорит, что сделать.
rem Куда - server\server.conf, как - server\server.sh deploy. Без входа по ключу пароль root сервера
rem спросят дважды - один раз настройте «Сервер - вход без пароля.bat».
set "DISTRO=Ubuntu-24.04"
title SupplyBro - отправить версию на сервер
wsl -d %DISTRO% --cd "%~dp0." -- bash server/server.sh deploy
echo.
echo  Нажмите любую клавишу, чтобы закрыть окно.
pause >nul
