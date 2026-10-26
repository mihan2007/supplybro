@echo off
chcp 65001 >nul
setlocal
rem Остановить сайт SupplyBro. Ubuntu продолжит работать, останавливается только сайт.
rem Запустить снова - «Запустить SupplyBro.bat».
set "DISTRO=Ubuntu-24.04"
title SupplyBro - остановка
echo.
echo  Останавливаю сайт SupplyBro...
wsl -d %DISTRO% --cd "%~dp0." -- docker compose down
echo.
echo  Готово. Запустить снова - «Запустить SupplyBro.bat».
timeout /t 5 >nul
