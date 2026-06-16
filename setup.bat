@echo off
echo ============================================================
echo  Site Inspection Photo Processor — Windows Setup
echo ============================================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python 3.10+ from https://python.org
    pause
    exit /b 1
)

echo [1/3] Creating virtual environment...
python -m venv venv
if errorlevel 1 (
    echo ERROR: Could not create virtual environment.
    pause
    exit /b 1
)

echo [2/3] Installing dependencies...
call venv\Scripts\activate.bat
pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: pip install failed. Check your internet connection.
    pause
    exit /b 1
)

echo [3/3] Creating run shortcut...
echo @echo off > run.bat
echo call "%~dp0venv\Scripts\activate.bat" >> run.bat
echo python "%~dp0main.py" >> run.bat
echo pause >> run.bat

echo.
echo ============================================================
echo  Setup complete!
echo  Double-click run.bat to launch the application.
echo ============================================================
pause
