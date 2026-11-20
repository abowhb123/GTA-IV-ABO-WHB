@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (echo ثبت Python 3.12 من python.org وفعّل خيار Add to PATH & pause & exit /b)
if not exist .venv (
  echo تجهيز البرنامج لأول مرة...
  python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt
)
start "" .venv\Scripts\pythonw main.py
