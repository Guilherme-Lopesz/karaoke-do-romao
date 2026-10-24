@echo off
title Karaoke - iniciador
cd /d "%~dp0"

rem Link para editar o karaoke-config.json no GitHub (ajuste se o nome do repositorio for outro)
set "GITHUB_EDIT=https://github.com/Guilherme-Lopesz/karaoke-do-romao/edit/main/karaoke-config.json"

if not exist karaoke_server.py goto sem_servidor

python --version >nul 2>nul && set "PY=python" || set "PY=py"

where cloudflared >nul 2>nul
if errorlevel 1 goto sem_cloudflared

echo [1/4] Abrindo o servidor do karaoke...
start "Servidor do Karaoke" cmd /k %PY% karaoke_server.py
timeout /t 3 /nobreak >nul

echo [2/4] Abrindo o tunel...
del tunel.log >nul 2>nul
start "Tunel Cloudflare" cmd /k cloudflared tunnel --url http://localhost:5000 --logfile tunel.log

echo [3/4] Aguardando o endereco do tunel, pode levar ate 40 segundos...
powershell -NoProfile -Command "$u='';for($i=0;$i -lt 40 -and -not $u;$i++){Start-Sleep 1;$m=Select-String -Path tunel.log -Pattern 'https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com' -List -ErrorAction SilentlyContinue;if($m){$u=$m.Matches[0].Value}};if($u){Set-Clipboard $u;Write-Host '';Write-Host ('ENDERECO DO TUNEL (ja copiado): '+$u)}else{Write-Host '';Write-Host 'Nao achei o endereco. Procure https://....trycloudflare.com na janela do tunel.'}"

echo [4/4] Abrindo o painel do admin e o GitHub...
start "" http://localhost:5000/admin
start "" "%GITHUB_EDIT%"

echo.
echo ============================================================
echo  FALTA SO UMA COISA:
echo  1. No GitHub que abriu, clique no lapis e troque o valor de
echo     "api" pelo endereco acima, ja copiado, e use Ctrl+V.
echo     Deve ficar: "api": "https://....trycloudflare.com",
echo  2. Clique em Commit changes e espere 1 a 2 minutos.
echo  3. Teste pelo celular: abra o site e peca uma musica.
echo.
echo  Painel do admin: http://localhost:5000/admin
echo  NAO feche as janelas Servidor e Tunel durante a festa.
echo ============================================================
echo.
pause
exit /b

:sem_servidor
echo ERRO: karaoke_server.py nao esta nesta pasta.
echo Coloque o iniciar.bat na mesma pasta do karaoke_server.py.
pause
exit /b

:sem_cloudflared
echo ERRO: o cloudflared nao foi encontrado.
echo Instale com: winget install --id Cloudflare.cloudflared
echo Depois feche esta janela e abra o iniciar.bat de novo.
pause
exit /b
