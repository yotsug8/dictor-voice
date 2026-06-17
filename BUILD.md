# Сборка из исходников

Инструкция для тех, кто хочет собрать `Diktor.exe` самостоятельно из исходного кода.
Нужна ОС **Windows**.

**Какой Python:**
- Только обычные голоса диктора → подойдёт **Python 3.12**.
- Нужны кастомные голоса персонажей (**RVC**) → обязателен **Python 3.10**.
  Библиотека `rvc-python` тянет старые `fairseq`/`numpy`, которые не собираются
  на 3.11/3.12. На 3.10 всё ставится из готовых wheel.

## 1. Установить Python

Скачай нужную версию с https://www.python.org/downloads/windows/ — файл «Windows installer (64-bit)»:
- без RVC — Python 3.12;
- с RVC — **Python 3.10** (например 3.10.11).

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

## 3a. (Необязательно) Кастомные голоса персонажей — RVC

Нужно только если хочешь голоса персонажей через папку `voices/`. Без этого
программа работает на обычных голосах диктора.

**Требуется Python 3.10** (см. начало инструкции) и видеокарта **NVIDIA**.

Установка:
pip install rvc-python

- Если `pip` начнёт собирать `fairseq` из исходников и ругнётся на отсутствие
  компилятора — поставь **Microsoft C++ Build Tools**
  (https://visualstudio.microsoft.com/visual-cpp-build-tools/), при установке выбери
  «Разработка классических приложений на C++», затем повтори команду.
- При первом использовании RVC докачивает базовые модели (HuBERT, RMVPE) из интернета (~400 МБ).
- Модели голосов (.pth, по желанию .index) скачиваются с weights.gg / AI Hub и кладутся в папку `voices/`.
  Файл .index класть рядом с .pth — он заметно улучшает качество.
- На CPU конверсия слишком медленная для реального времени — нужна NVIDIA.

Проверка после установки:
python -c "from rvc_python.infer import RVCInference; print('rvc ok')"

## 4. Собрать приложение

Положи в одну папку файлы: `golos_diktora_gui.py`, `sborka_exe.bat`, `hook-webrtcvad.py`, `icon.ico`.
Затем запусти сборщик:
sborka_exe.bat

`sborka_exe.bat` сам проверяет, установлен ли `rvc-python` под Python 3.10:
- Если да — собирает под Python 3.10 и добавляет голоса персонажей (RVC) в сборку.
- Если нет — собирает обычную версию без RVC (как раньше).

Сборка идёт 10–30 минут (с RVC дольше — добавляются крупные библиотеки fairseq/faiss/numba).
Готовое приложение появится в папке `dist\Diktor\` — запускай `Diktor.exe` оттуда (не вынимай его из папки).

### Если RVC-сборка не запускается / ругается на отсутствующий модуль

PyInstaller не всегда сам находит все скрытые зависимости таких библиотек, как
`fairseq`, `hydra-core`, `omegaconf`, `faiss`. Если собранный `Diktor.exe` при
запуске или при выборе голоса `[Персонаж]` пишет в окне консоли/логе что-то вида
`ModuleNotFoundError: No module named 'X'` или `No package metadata was found for X`:

1. Открой `sborka_exe.bat`.
2. В строке `set RVC_FLAGS=...` добавь `--collect-all X` (где `X` — имя
   недостающего модуля/пакета).
3. Удали папки `build`, `dist` и файл `Diktor.spec`, запусти `sborka_exe.bat` заново.

Это нормальный процесс для PyInstaller-сборок с такими библиотеками — обычно
хватает 1–3 таких итераций.

## Примечания

- Перед повторной сборкой удаляй папки `build`, `dist` и файл `Diktor.spec`, иначе подхватятся старые настройки.
- `hook-webrtcvad.py` — заглушка для PyInstaller, без неё сборка прерывается. Должна лежать рядом.
- Виртуальный микрофон **VB-Cable** в сборку не входит и устанавливается отдельно: https://vb-audio.com/Cable/
- При первом запуске собранное приложение докачивает модель распознавания из интернета.
- При первом использовании RVC-голоса собранное приложение докачивает базовые модели (HuBERT, RMVPE) из интернета (~400 МБ) — как и в режиме «из исходников».
