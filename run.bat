@echo off
setlocal
cd /d "%~dp0"

if not exist "backend\venv\Scripts\python.exe" (
    echo [InsightFlow] Create the Python environment first:
    echo   python -m venv backend\venv
    echo   backend\venv\Scripts\python.exe -m pip install -r backend\requirements.txt
    exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
    echo [InsightFlow] Node.js/npm is required for the React frontend.
    exit /b 1
)

if not exist "frontend\node_modules" (
    echo [InsightFlow] Installing React frontend dependencies...
    pushd frontend
    call npm install
    if errorlevel 1 exit /b 1
    popd
)

REM Always rebuild: legacy agent logic and UI overrides must stay in sync.
echo [InsightFlow] Building React + Vite + shadcn frontend...
pushd frontend
call npm run build
if errorlevel 1 exit /b 1
popd

"backend\venv\Scripts\python.exe" run.py
