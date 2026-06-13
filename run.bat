@echo off
cd /d "%~dp0"
echo Starting QR Code Ordering System...
start "" "http://localhost:3000"
"C:\Users\USER\anaconda3\python.exe" app.py
pause
