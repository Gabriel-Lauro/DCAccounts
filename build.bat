@echo off
setlocal
cd /d "%~dp0"

echo === DC Accounts: build ===
if not exist .venv (
    python -m venv .venv || goto :erro
)
call .venv\Scripts\activate || goto :erro
python -m pip install --upgrade pip -q
pip install -r requirements.txt || goto :erro

echo.
echo [1/3] Gerando icone (mesmo desenho do icone da bandeja)...
python main.py --make-icon icon.ico || goto :erro

echo [2/3] Compilando com PyInstaller...
pyinstaller --noconfirm --clean --onefile --windowed --name DCAccounts --icon icon.ico main.py || goto :erro

echo [3/3] Copiando o .exe para a raiz desta pasta...
copy /Y "dist\DCAccounts.exe" "%~dp0DCAccounts.exe" >nul
if errorlevel 1 (
    echo.
    echo ERRO: nao consegui copiar. Feche o DCAccounts.exe ^(menu da bandeja ^> Sair^) e rode de novo.
    goto :fim
)

echo.
echo Pronto: %~dp0DCAccounts.exe
echo Os dados ^(accounts.json e a pasta profiles^) ficam ao lado do .exe.
goto :fim

:erro
echo.
echo FALHOU. Veja as mensagens acima.

:fim
pause
