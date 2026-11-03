@echo off
title LocalKTV
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-karaoke.ps1" %*
echo.
pause
