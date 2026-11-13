@echo off
chcp 65001 >nul 2>&1
title NEXUS STUDIO - 一键启动（平台 + ComfyUI）
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_services.ps1"
echo.
pause