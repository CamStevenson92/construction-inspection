@echo off
cd /d "%~dp0"
echo Starting Site Inspection Web App...
echo Open your browser to: http://localhost:5000
echo On your phone (same Wi-Fi): http://%COMPUTERNAME%:5000
echo.
echo Press Ctrl+C to stop.
echo.
python web_app.py
pause
