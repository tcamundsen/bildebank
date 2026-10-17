@echo off
setlocal

set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo Fant ikke "%PYTHON%".
    echo Kjor denne filen fra den installerte programmappen til Bildebank.
    pause
    exit /b 1
)

"%PYTHON%" "%~dp0tools\diagnose_face_scan.py" %*
set "DIAGNOSE_EXIT_CODE=%ERRORLEVEL%"
pause
exit /b %DIAGNOSE_EXIT_CODE%
