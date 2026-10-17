@echo off
setlocal

rem Keep startup errors in this console instead of spawning a separate launcher.
set "BILDEBANK_WINDOWS_LAUNCHER_CHILD=1"
call "%~dp0bin\bildebank.cmd" start
set "START_EXIT_CODE=%ERRORLEVEL%"

if not "%START_EXIT_CODE%"=="0" (
    echo.
    echo Bildebank kunne ikke starte. Se feilmeldingen over.
    pause
)

exit /b %START_EXIT_CODE%
