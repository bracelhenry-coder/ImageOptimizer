@echo off
echo ========================================
echo  ImageOptimizer - Build App
echo ========================================
echo.

echo Checking for a running ImageOptimizer.exe...
taskkill /F /IM ImageOptimizer.exe >nul 2>&1

echo.
echo Building application folder...
pyinstaller --noconfirm --windowed ^
    --name ImageOptimizer ^
    --add-data "style.qss;." ^
    --hidden-import PySide2.QtXml ^
    texture_optimizer_ui.py

if errorlevel 1 (
    echo.
    echo ERROR: Build failed. See output above.
    pause
    exit /b 1
)

echo.
echo ========================================
echo  Done!
echo  Your app is at: dist\ImageOptimizer\ImageOptimizer.exe
echo  Share the whole dist\ImageOptimizer folder.
echo ========================================
pause
