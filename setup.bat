@echo off
echo ========================================
echo  ImageOptimizer - One-Time Setup
echo ========================================
echo.

echo Installing required packages...
pip install pyinstaller PySide2 Pillow
if errorlevel 1 (
    echo.
    echo ERROR: Install failed. Make sure Python and pip are available.
    pause
    exit /b 1
)

echo.
echo Setup complete.
echo You can now run build.bat whenever you want to rebuild.
pause
