@echo off
title Karaoke - iniciador
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (set PY=python) else (set PY=py)

echo Abrindo o servidor do karaoke...
start "Servidor do Karaoke" cmd /k %PY% karaoke_server.py

timeout /t 3 /nobreak >nul

echo Abrindo o tunel...
start "Tunel Cloudflare" cmd /k cloudflared tunnel --url http://localhost:5000

echo.
echo ============================================================
echo  PROXIMO PASSO:
echo  Na janela "Tunel Cloudflare", copie o endereco
echo  https://....trycloudflare.com
echo  e cole no campo "api" do karaoke-config.json no GitHub.
echo ============================================================
echo.
pause
