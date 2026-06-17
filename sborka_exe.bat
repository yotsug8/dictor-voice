@echo off
echo ============================================
echo   Sborka Golos Diktora v EXE
echo ============================================
echo.

echo Ochishchau staruyu sborku (build, dist, Diktor.spec)...
if exist build rd /s /q build
if exist dist rd /s /q dist
if exist Diktor.spec del /q Diktor.spec
echo.

set PYCMD=py -3.10
set RVC_FLAGS=
py -3.10 -c "import rvc_python" 2>nul
if not errorlevel 1 (
    echo Naiden rvc-python - sobirau s podderzhkoy golosov personazhey (RVC).
    set RVC_FLAGS=--collect-all rvc_python --collect-all fairseq --collect-all hydra --collect-all omegaconf --collect-all antlr4 --collect-all faiss --collect-all librosa --collect-all numba --collect-all llvmlite --collect-all torchcrepe --collect-all pyworld
) else (
    echo rvc-python ne naiden - sobirau obychnuyu versiyu, bez golosov personazhey.
    echo Esli nuzhen RVC v sborke - smotri razdel 3a v BUILD.md.
)

echo.
echo [1/2] Ustanavlivayu PyInstaller...
%PYCMD% -m pip install pyinstaller

echo.
echo [2/2] Sobirayu EXE (eto dolgo, mozhet zanyat 10-30 minut)...
%PYCMD% -m PyInstaller --onedir --windowed --name Diktor ^
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
 %RVC_FLAGS% ^
 golos_diktora_gui.py

echo.
echo ============================================
echo   Gotovo! EXE lezhit v papke: dist\Diktor\Diktor.exe
echo ============================================
pause
