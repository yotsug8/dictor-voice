# Сборка из исходников

Инструкция для тех, кто хочет собрать `Diktor.exe` самостоятельно из исходного кода.
Нужна ОС **Windows** и **Python 3.12**.

## 1. Установить Python

Скачай Python 3.12 с https://www.python.org/downloads/windows/ — файл «Windows installer (64-bit)».
При установке обязательно поставь галочку **«Add python.exe to PATH»**.

## 2. Установить зависимости

В командной строке выполни:
pip install RealtimeSTT edge-tts sounddevice soundfile numpy faster-whisper silero-vad packaging customtkinter deep-translator pystray pillow keyboard

## 3. Установить PyTorch

Версия приложения (NVIDIA или CPU) определяется тем, какой PyTorch установлен.

- С видеокартой **NVIDIA** (быстрее):
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
- **Без** видеокарты NVIDIA (работает у всех, медленнее):
pip install torch torchaudio

Проверить, какой режим получится:
python -c "import torch; print(torch.cuda.is_available())"
`True` — будет использоваться видеокарта, `False` — процессор.

## 4. Собрать приложение

Положи в одну папку файлы: `golos_diktora_gui.py`, `sborka_exe.bat`, `hook-webrtcvad.py`, `icon.ico`.
Затем запусти сборщик:
sborka_exe.bat

Сборка идёт 10–20 минут. Готовое приложение появится в папке `dist\Diktor\` — запускай `Diktor.exe` оттуда (не вынимай его из папки).

## Примечания

- Перед повторной сборкой удаляй папки `build`, `dist` и файл `Diktor.spec`, иначе подхватятся старые настройки.
- `hook-webrtcvad.py` — заглушка для PyInstaller, без неё сборка прерывается. Должна лежать рядом.
- Виртуальный микрофон **VB-Cable** в сборку не входит и устанавливается отдельно: https://vb-audio.com/Cable/
- При первом запуске собранное приложение докачивает модель распознавания из интернета.
