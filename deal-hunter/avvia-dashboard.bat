@echo off
rem Doppio clic: prepara l'ambiente la prima volta e apre la dashboard nel browser.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Prima installazione, attendi un paio di minuti...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -e ".[browser]"
  .venv\Scripts\python -m playwright install chromium
)
if not exist config\config.yaml copy config\config.example.yaml config\config.yaml >nul
if not exist .env copy .env.example .env >nul
.venv\Scripts\dealhunter dashboard %*
pause
