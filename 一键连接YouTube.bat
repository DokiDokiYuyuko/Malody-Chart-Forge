@echo off
chcp 65001 >nul
cd /d "%~dp0"
node "%~dp0tools\youtube_login.cjs" --plain-login
pause
