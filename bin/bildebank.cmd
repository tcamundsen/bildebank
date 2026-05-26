@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "REPO_DIR=%%~fI"
set "PYTHON=%REPO_DIR%\.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo Fant ikke "%PYTHON%".
    echo Opprett venv og installer prosjektet først:
    echo   py -3.13 -m venv .venv
    echo   .\.venv\Scripts\python.exe -m pip install -e .
    exit /b 1
)

"%PYTHON%" -m bildebank %*
