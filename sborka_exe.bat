@echo off
echo ============================================
echo   Sborka Golos Diktora v EXE
echo ============================================
echo.

echo [1/2] Ustanavlivayu PyInstaller...
pip install pyinstaller

echo.
echo [2/2] Sobirayu EXE (eto dolgo, mozhet zanyat 10-20 minut)...
pyinstaller --onedir --windowed --name Diktor ^
 --icon=icon.ico ^
 --add-data "icon.ico;." ^
 --additional-hooks-dir=. ^
 --collect-all torch ^
 --collect-all faster_whisper ^
 --collect-all ctranslate2 ^
 --collect-all silero_vad ^
 --collect-all RealtimeSTT ^
 --collect-all edge_tts ^
 --collect-all customtkinter ^
 --collect-all deep_translator ^
 --collect-all pystray ^
 --collect-all keyboard ^
 golos_diktora_gui.py

echo.
echo ============================================
echo   Gotovo! EXE lezhit v papke: dist\Diktor\Diktor.exe
echo ============================================
pause
