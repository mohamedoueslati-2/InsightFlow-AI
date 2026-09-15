@echo off
setlocal
cd /d "%~dp0"
if not exist "backend\venv\Scripts\python.exe" (
  echo Create Python environment first.
  exit /b 1
)
if not exist "frontend\node_modules" (
  pushd frontend
  call npm install
  popd
)
start "InsightFlow API" cmd /k "backend\venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload"
pushd frontend
call npm run dev
popd
