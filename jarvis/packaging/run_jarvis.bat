@echo off
rem Starts JARVIS from a source checkout on Windows.
setlocal
if "%JARVIS_HOME%"=="" set "JARVIS_HOME=%APPDATA%\JARVIS"
if "%JARVIS_HOST%"=="" set "JARVIS_HOST=127.0.0.1"
if "%JARVIS_PORT%"=="" set "JARVIS_PORT=8765"
python -m jarvis.interfaces.desktop.app --host %JARVIS_HOST% --port %JARVIS_PORT%
start "" "http://%JARVIS_HOST%:%JARVIS_PORT%"
endlocal
