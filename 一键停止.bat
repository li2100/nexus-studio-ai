@echo off
chcp 65001 >nul
title NEXUS STUDIO - 一键停止
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_services.ps1"
echo.
pause