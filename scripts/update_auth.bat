@echo off
REM ============================================================
REM update_auth.bat - Update IIFL Auth on NSE Ingestor (Windows)
REM ============================================================
REM Usage: update_auth.bat YOUR_AUTH_CODE
REM
REM What it does:
REM   1. Takes your auth code
REM   2. Calls IIFL API to exchange it for a JWT
REM   3. Sends both auth code + JWT to the VM
REM   4. Restarts the ingestor container
REM
REM This is just a shortcut to: python update_auth.py YOUR_AUTH_CODE
REM ============================================================

if "%1"=="" (
    echo.
    echo   Usage: update_auth.bat YOUR_AUTH_CODE
    echo.
    echo   Steps:
    echo     1. Open this URL in your browser:
    echo        https://markets.iiflcapital.com/?v=1^&appkey=YOUR_APP_KEY^&redirecturl=http://localhost:3000/callback
    echo        (Replace YOUR_APP_KEY with the IIFL_APP_KEY value from your .env file)
    echo     2. Log in with IIFL credentials
    echo     3. After redirect, copy the 'code' from URL bar:
    echo        http://localhost:3000/callback?code=XXXXXXXXXX
    echo                                           ^^^^^^^^^^ copy this
    echo     4. Run: update_auth.bat PASTE_CODE_HERE
    echo.
    exit /b 1
)

REM Run the Python script which handles auth code -> JWT -> VM update
python "%~dp0update_auth.py" %1
