@echo off
cd /d "%~dp0"
echo Iniciando VIGIA...

rem Usa el lanzador "py" si existe (evita el acceso directo roto de la Microsoft Store)
set "PY=python"
where py >nul 2>nul && set "PY=py -3"

if not exist .venv\Scripts\python.exe %PY% -m venv .venv
.venv\Scripts\python -m pip install -q --disable-pip-version-check -r requirements.txt

rem Intervalo corto para que la demo local se vea viva (en produccion: 30 s)
set "VIGIA_INTERVAL=15"
start "" http://127.0.0.1:8090
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8090
