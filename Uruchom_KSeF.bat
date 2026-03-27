@echo off
chcp 65001 >nul 2>&1
title KSeF Konwerter Faktur

:: ------------------------------------------------------------------
:: Launcher for KSeF GUI — double-click this file to start
:: ------------------------------------------------------------------

:: Try "python", "python3", "py" — with version >= 3.9 gate
:: Using "if not errorlevel 1" for reliable exit-code checking in cmd.exe
python -c "import sys; assert sys.version_info >= (3, 9)" >nul 2>&1
if not errorlevel 1 (
    python "%~dp0ksef_gui.py"
    goto :end
)

python3 -c "import sys; assert sys.version_info >= (3, 9)" >nul 2>&1
if not errorlevel 1 (
    python3 "%~dp0ksef_gui.py"
    goto :end
)

py -c "import sys; assert sys.version_info >= (3, 9)" >nul 2>&1
if not errorlevel 1 (
    py "%~dp0ksef_gui.py"
    goto :end
)

:: Python not found or too old
echo.
echo ============================================================
echo   BLAD: Python 3.9+ nie zostal znaleziony!
echo.
echo   Zainstaluj Python 3.9 lub nowszy ze strony:
echo   https://www.python.org/downloads/
echo.
echo   WAZNE: Podczas instalacji zaznacz opcje:
echo     [x] Add Python to PATH
echo     [x] tcl/tk and IDLE
echo ============================================================
echo.
pause

:end
