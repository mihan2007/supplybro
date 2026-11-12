@echo off
chcp 65001 >nul
setlocal
rem Терминал сервера сразу в папке сайта (0.24.0): здесь работают команды docker compose.
rem Выйти - команда exit.
set "DISTRO=Ubuntu-24.04"
title SupplyBro - терминал сервера
wsl -d %DISTRO% --cd "%~dp0." -- bash server/server.sh console
if errorlevel 1 pause
