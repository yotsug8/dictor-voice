# Сборка из исходников

Инструкция для тех, кто хочет собрать `Diktor.exe` самостоятельно из исходного кода.
Нужна ОС **Windows**.

## 1. Установить Python

Скачай Python 3.10.11 с https://www.python.org/downloads/windows/ — файл «Windows installer (64-bit)».
Бери именно **3.10.11** (или новее в ветке 3.10.x): в 3.10.0 есть баг, из-за которого сборщик PyInstaller
падает с ошибкой `IndexError: tuple index out of range`.

При установке обязательно поставь галочку **«Add python.exe to PATH»**.

## 2. Установить зависимости

В командной строке выполни:
py -3.10 -m pip install RealtimeSTT edge-tts sounddevice soundfile numpy faster-whisper silero-vad packaging customtkinter deep-translator pystray pillow keyboard

## 3. Установить PyTorch

Версия приложения (NVIDIA или CPU) определяется тем, какой PyTorch установлен.

- С видеокартой **NVIDIA** (быстрее):
py -3.10 -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
- **Без** видеокарты NVIDIA (работает у всех, медленнее):
py -3.10 -m pip install torch torchaudio

Проверить, какой режим получится:
py -3.10 -c "import torch; print(torch.cuda.is_available())"
`True` — будет использоваться видеокарта, `False` — процессор.

## 3a. (Необязательно) Кастомные голоса персонажей — RVC

Нужно только если хочешь голоса персонажей через папку `voices/`. Без этого
программа работает на обычных голосах диктора.

Требуется видеокарта **NVIDIA** (на CPU конверсия слишком медленная для
реального времени).

Установка:
py -3.10 -m pip install rvc-python

- Если `pip` начнёт собирать `fairseq` из исходников и ругнётся на отсутствие
  компилятора — поставь **Microsoft C++ Build Tools**
  (https://visualstudio.microsoft.com/visual-cpp-build-tools/), при установке выбери
  «Разработка классических приложений на C++», затем повтори команду.
- При первом использовании RVC докачивает базовые модели (HuBERT, RMVPE) из интернета (~400 МБ).
- Модели голосов (.pth, по желанию .index) кладутся в папку `voices/`.
  Файл .index класть рядом с .pth — он заметно улучшает качество.

Проверка после установки:
py -3.10 -c "from rvc_python.infer import RVCInference; print('rvc ok')"

## 4. Собрать приложение

Положи в одну папку файлы: `golos_diktora_gui.py`, `sborka_exe.bat`, `hook-webrtcvad.py`, `icon.ico`.
Затем запусти сборщик:
sborka_exe.bat

`sborka_exe.bat` сам проверяет, установлен ли `rvc-python`:
- Если да — добавляет голоса персонажей (RVC) в сборку.
- Если нет — собирает обычную версию без RVC.

Сборка идёт 10–30 минут (с RVC дольше — добавляются крупные библиотеки fairseq/faiss/numba).
Готовое приложение появится в папке `dist\Diktor\` — запускай `Diktor.exe` оттуда (не вынимай его из папки).

### Если собранный `Diktor.exe` пишет об отсутствующем модуле или пути

PyInstaller не всегда сам находит все скрытые зависимости и файлы ресурсов. Если
собранный `Diktor.exe` при запуске или при выборе голоса `[Персонаж]` пишет в
окне консоли/логе что-то вида `ModuleNotFoundError: No module named 'X'`,
`No package metadata was found for X` или `[WinError 3] ... не удаётся найти
указанный путь: '...\_internal\X\...'`:

1. Открой `sborka_exe.bat`.
2. Добавь `--collect-all X` (где `X` — имя недостающего модуля/пакета, например
   `pvporcupine`). Для библиотек RVC добавляй в строку `set RVC_FLAGS=...`,
   для остальных — в список флагов команды `PyInstaller` ниже.
3. Запусти `sborka_exe.bat` заново (старые `build`, `dist`, `Diktor.spec` он удалит сам).

Это нормальный процесс для PyInstaller-сборок с такими библиотеками — обычно
хватает 1–3 таких итераций.

## Примечания

- `sborka_exe.bat` сам удаляет старые `build`, `dist` и `Diktor.spec` перед каждой сборкой.
- `hook-webrtcvad.py` — заглушка для PyInstaller, без неё сборка прерывается. Должна лежать рядом.
- Виртуальный микрофон **VB-Cable** в сборку не входит и устанавливается отдельно: https://vb-audio.com/Cable/
- При первом запуске собранное приложение докачивает модель распознавания из интернета.
- При первом использовании RVC-голоса собранное приложение докачивает базовые модели (HuBERT, RMVPE) из интернета (~400 МБ) — как и в режиме «из исходников».
