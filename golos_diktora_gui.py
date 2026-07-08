import io
import os
import sys
import json
import asyncio
import datetime
import threading
import queue
import time

# В собранном .exe (PyInstaller) урезанный модуль site не создаёт интерактивные
# встроенные help/exit/quit/license/credits/copyright. Библиотеки RVC ссылаются
# на них (видели "name 'help' is not defined"), что роняет конверсию. Возвращаем
# их все сразу, чтобы не ловить такие ошибки по одной.
import builtins as _builtins

if not hasattr(_builtins, "help"):
    try:
        import pydoc
        _builtins.help = pydoc.help
    except Exception:
        _builtins.help = lambda *a, **k: None

class _ExitProxy:
    def __repr__(self):
        return "Use exit() or Ctrl-Z plus Return to exit"
    def __call__(self, code=None):
        raise SystemExit(code)

for _name in ("exit", "quit"):
    if not hasattr(_builtins, _name):
        setattr(_builtins, _name, _ExitProxy())

class _PrinterProxy:
    def __repr__(self):
        return ""
    def __call__(self, *a, **k):
        return None

for _name in ("license", "credits", "copyright"):
    if not hasattr(_builtins, _name):
        setattr(_builtins, _name, _PrinterProxy())

# В сборке --windowed (без консоли) sys.stdout/sys.stderr — None. Сторонний код
# (например, multiprocessing при выводе traceback из дочернего процесса) вызывает
# .write() не проверяя это и роняет приложение AttributeError'ом, маскируя
# настоящую ошибку. Подставляем заглушки, которые просто проглатывают вывод.
class _NullStream:
    def write(self, s="", *a, **k):
        return len(s) if isinstance(s, (str, bytes, bytearray)) else 0
    def flush(self):
        pass
    def isatty(self):
        return False

if sys.stdout is None:
    sys.stdout = _NullStream()
if sys.stderr is None:
    sys.stderr = _NullStream()

import numpy as np
import sounddevice as sd
import soundfile as sf
import edge_tts
import customtkinter as ctk


# ---- palette ----
# BG/CARD/CARD2/FIELD — четыре уровня "глубины" (фон окна -> вкладки -> приподнятые
# плашки внутри них -> поля ввода), чтобы интерфейс не выглядел одной плоской заливкой
BG="#10101a"; CARD="#1a1a28"; CARD2="#242438"; FIELD="#2c2c42"; TEXT="#eef0f8"; SUB="#9aa0b4"
ACCENT="#7c8cff"; ACC_HOV="#6675f0"; ACCENT_SOFT="#32325a"; GREEN="#3ecf8e"; GREEN_H="#34b87d"
RED="#f0617f"; RED_H="#db5071"; YELLOW="#f5c860"; GREY="#5b5f70"

VOICES = {
    "Дмитрий (мужской)": "ru-RU-DmitryNeural",
    "Светлана (женский)": "ru-RU-SvetlanaNeural",
}
MODELS = ["tiny", "base", "small", "medium"]
MODEL_BEAM = {"tiny": 1, "base": 1, "small": 3, "medium": 5}
SPEEDS = {"Медленно": "-25%", "Обычная": "+0%", "Быстро": "+25%"}
# display -> (код перевода, голос для озвучки перевода)
LANGUAGES = {
    "Выкл (русский)": (None, None),
    "Английский": ("en", "en-US-GuyNeural"),
    "Немецкий": ("de", "de-DE-KillianNeural"),
    "Французский": ("fr", "fr-FR-HenriNeural"),
    "Испанский": ("es", "es-ES-AlvaroNeural"),
    "Итальянский": ("it", "it-IT-DiegoNeural"),
    "Португальский": ("pt", "pt-BR-AntonioNeural"),
    "Польский": ("pl", "pl-PL-MarekNeural"),
    "Японский": ("ja", "ja-JP-KeitaNeural"),
    "Корейский": ("ko", "ko-KR-InJoonNeural"),
    "Китайский": ("zh-CN", "zh-CN-YunxiNeural"),
    "Турецкий": ("tr", "tr-TR-AhmetNeural"),
}
TEST_PHRASE = "Проверка связи. Это голос диктора."
WHISPER_PROMPT = "Привет. Да, конечно. Хорошо, понятно. Спасибо. Сегодня хорошая погода."
FONT = "Segoe UI"


def _base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _resource(name):
    return os.path.join(getattr(sys, "_MEIPASS", _base_dir()), name)


SETTINGS_FILE = os.path.join(_base_dir(), "settings.json")
VOICES_DIR = os.path.join(_base_dir(), "voices")
# базовый голос Edge TTS, поверх которого RVC накладывает тембр персонажа
RVC_BASE_VOICE = "ru-RU-DmitryNeural"
# через столько секунд после сбоя RVC снова пробуем конверсию (а не глушим навсегда)
RVC_RETRY_COOLDOWN = 60


class DiktorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Голос Диктора")
        self.root.geometry("680x880")
        self.root.minsize(620, 800)
        self.root.configure(fg_color=BG)

        self.running = False
        self.muted = False
        self.recorder = None
        self._recorder_lock = threading.Lock()
        self._recorder_restart_pending = False
        # последний «финальный» статус (Остановлено/Ошибка) для _reassert_status
        self._desired_status = None
        # фоновые потоки, в которых отрабатывает rec.shutdown() старых
        # рекордеров — рабочий поток дожидается ВСЕХ их перед тем как строить
        # новый (см. _join_recorder_shutdown). Доступ только под _recorder_lock.
        self._recorder_shutdown_threads = []
        self.device_map = {}
        # сериализует целиком синтез->RVC->проигрывание: «Тест», озвучку набранного
        # текста и основной цикл распознавания запускают независимые потоки, и без
        # общего замка на весь конвейер (а не только на сам sd.play) они могли
        # озвучивать разные фразы почти одновременно — пользователь слышал, как
        # будто говорят два диктора сразу
        self._speak_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        self._testing = False
        self._saying = False
        self._redraw_after_id = None
        self._save_after_id = None
        self._settings_write_warned = False
        self._settings_load_error = None
        self._vol_zero_warned = False
        # RVC невозможен без видеокарты NVIDIA — определив это один раз, больше
        # не дёргаем конверсию и не засоряем лог повторными попытками
        self._rvc_no_cuda = False
        self.tray = None
        self.ui_queue = queue.Queue()
        self.mic_level = 0.0
        self._rvc = None
        self._rvc_loaded_path = None
        # путь RVC-модели -> момент последнего сбоя (time.monotonic). После
        # сбоя голос временно играет базовым тембром, но через RVC_RETRY_COOLDOWN
        # секунд снова пробуется — разовый сбой сети не отключает персонажа навсегда
        self._rvc_failed_paths = {}
        self._rvc_lock = threading.Lock()
        self.rvc_voices = self._scan_rvc_voices()
        self.cfg = self._load_settings()
        profiles = self.cfg.get("profiles", {})
        self.profiles = profiles if isinstance(profiles, dict) else {}

        self._build()
        self.root.after(80, self._drain)
        self._setup_hotkey()
        self._setup_tray()
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        # переключение вкладок и (особенно) изменение размера окна иногда
        # оставляют на экране «хвосты» старой отрисовки округлых рамок —
        # принудительный update_idletasks() после того, как пользователь
        # перестал тащить рамку/кликать по вкладкам, убирает их
        self.root.bind("<Configure>", self._on_root_configure)

    # ---------- settings ----------
    def _load_settings(self):
        if not os.path.exists(SETTINGS_FILE):
            return {}
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            # файл есть, но не читается/повреждён — сохраняем копию и сообщим
            # пользователю после сборки интерфейса (здесь self.log ещё нет)
            try:
                os.replace(SETTINGS_FILE, SETTINGS_FILE + ".corrupt")
            except Exception:
                pass
            self._settings_load_error = ("Файл настроек был повреждён — восстановлены значения "
                                         "по умолчанию (старый сохранён рядом как "
                                         "settings.json.corrupt).")
            return {}

    def _sync_settings_snapshot(self):
        """Снимок текущих настроек в обычные Python-атрибуты.

        Tk-переменные (StringVar/IntVar) нельзя безопасно читать из рабочих
        потоков: .get() заходит в Tcl, который НЕ потокобезопасен, и на Windows
        это давало случайные сбои/мусор («много мелких багов»). Поэтому рабочие
        потоки (распознавание, синтез, проигрывание) читают только эти
        атрибуты, а обновляем их строго в главном потоке Tk."""
        try:
            self._cur_voice = self.voice_var.get()
            self._cur_model = self.model_var.get()
            self._cur_output = self.device_var.get()
            self._cur_speed = self.speed_var.get()
            self._cur_lang = self.lang_var.get()
            self._cur_vol = max(0, min(100, int(self.vol_var.get())))
            self._cur_pitch = max(-12, min(12, int(self.pitch_var.get())))
        except Exception:
            pass

    def _save_settings(self):
        # снимок делаем здесь, т.к. _save_settings вызывается из главного потока
        # при любом изменении настроек (трассировки, ползунки, старт, тест)
        self._sync_settings_snapshot()
        try:
            data = {
                "voice": self._cur_voice,
                "model": self._cur_model,
                "device": self._cur_output,
                "speed": self._cur_speed,
                "lang": self._cur_lang,
                "volume": self._cur_vol,
                "pitch": self._cur_pitch,
                "topmost": bool(self.topmost_var.get()),
                "profiles": self.profiles,
            }
            # пишем через временный файл + os.replace: если процесс/диск оборвётся
            # посреди записи, старый settings.json (со всеми профилями) уцелеет,
            # а не превратится в усечённый мусор
            tmp = SETTINGS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, SETTINGS_FILE)
        except Exception:
            # обычно нет прав на запись (установка в Program Files) — сообщаем
            # один раз, чтобы пользователь не удивлялся сбросу настроек
            if not self._settings_write_warned:
                self._settings_write_warned = True
                self._log("Не удалось сохранить настройки — вероятно, нет прав на запись в папку "
                          "программы. Настройки не сохранятся между запусками. Перенесите программу "
                          "в обычную папку (например, на Рабочий стол).")

    # ---------- ui ----------
    def _cap(self, parent, text):
        return ctk.CTkLabel(parent, text=text, text_color=SUB, font=(FONT, 12), anchor="w")

    def _menu(self, parent, variable, values, **kw):
        return ctk.CTkOptionMenu(parent, variable=variable, values=values,
                                 fg_color=FIELD, button_color=FIELD,
                                 button_hover_color="#3a3a54", text_color=TEXT,
                                 dropdown_fg_color=CARD, dropdown_hover_color=FIELD,
                                 dropdown_text_color=TEXT, corner_radius=10,
                                 font=(FONT, 12), dropdown_font=(FONT, 12), **kw)

    def _build(self):
        head = ctk.CTkFrame(self.root, fg_color="transparent")
        head.pack(fill="x", padx=26, pady=(16, 4))
        ctk.CTkLabel(head, text="🎙", text_color=ACCENT, fg_color=ACCENT_SOFT,
                     corner_radius=20, width=40, height=40, font=(FONT, 17)).pack(side="left")
        ctk.CTkLabel(head, text="Голос Диктора", text_color=TEXT,
                     font=(FONT, 23, "bold")).pack(side="left", padx=(12, 0))

        self.status_pill = ctk.CTkFrame(head, fg_color=CARD2, corner_radius=999, height=30)
        self.status_pill.pack(side="right")
        self.dot = ctk.CTkLabel(self.status_pill, text="●", text_color=GREY, font=(FONT, 13))
        self.dot.pack(side="left", padx=(14, 4), pady=4)
        self.status_lbl = ctk.CTkLabel(self.status_pill, text="Остановлено", text_color=SUB,
                                       font=(FONT, 12, "bold"))
        self.status_lbl.pack(side="left", padx=(0, 14), pady=4)

        ctk.CTkLabel(self.root, text="После паузы в речи программа озвучит сказанное голосом диктора",
                     text_color=SUB, font=(FONT, 11), anchor="w").pack(fill="x", padx=28)

        # высота задана явно и фиксирована (grid_propagate(False) ниже), иначе
        # CTkTabview меняет размер под содержимое текущей вкладки — при
        # переключении на вкладку с меньшим числом строк («Устройства») окно
        # дёргалось и всё расположенное ниже (кнопки, лог) прыгало на место
        tabs = ctk.CTkTabview(self.root, height=440, fg_color=CARD, corner_radius=18,
                              segmented_button_fg_color=FIELD,
                              segmented_button_selected_color=ACCENT,
                              segmented_button_selected_hover_color=ACC_HOV,
                              segmented_button_unselected_hover_color="#3a3a54",
                              text_color=TEXT, command=self._force_redraw)
        tabs.pack(fill="x", padx=26, pady=10)
        tabs.grid_propagate(False)
        try:
            tabs._segmented_button.configure(font=(FONT, 13, "bold"))
        except Exception:
            pass
        tab_voice = tabs.add("Голос")
        tab_dev = tabs.add("Устройства")
        tab_voice.columnconfigure(0, weight=1)
        tab_voice.columnconfigure(1, weight=1)
        tab_dev.columnconfigure(0, weight=1)
        tab_dev.columnconfigure(1, weight=1)

        def g(k, d, pool):
            v = self.cfg.get(k)
            return v if v in pool else d
        voice_names = list(VOICES) + list(self.rvc_voices)
        self.voice_var = ctk.StringVar(value=g("voice", list(VOICES)[0], voice_names))
        self.voice_var.trace_add("write", self._on_setting_change)
        self.model_var = ctk.StringVar(value=g("model", "small", MODELS))
        self.model_var.trace_add("write", self._on_model_change)
        self.speed_var = ctk.StringVar(value=g("speed", "Обычная", SPEEDS))
        self.speed_var.trace_add("write", self._on_setting_change)
        self.lang_var = ctk.StringVar(value=g("lang", list(LANGUAGES)[0], LANGUAGES))
        self.lang_var.trace_add("write", self._on_setting_change)
        try:
            vol0 = max(0, min(100, int(self.cfg.get("volume", 100))))
        except (TypeError, ValueError):
            vol0 = 100
        self.vol_var = ctk.IntVar(value=vol0)
        try:
            pitch0 = max(-12, min(12, int(self.cfg.get("pitch", 0))))
        except (TypeError, ValueError):
            pitch0 = 0
        self.pitch_var = ctk.IntVar(value=pitch0)

        devices = self._devices()
        self._cable_found = self._cable_present(devices)
        dev0 = self.cfg.get("device") if self.cfg.get("device") in devices else self._default_device(devices)
        self.device_var = ctk.StringVar(value=dev0)
        self.device_var.trace_add("write", self._on_setting_change)

        self.profile_var = ctk.StringVar(value="Без профиля")

        # --- вкладка «Голос» ---
        self._cap(tab_voice, "⭐  Профиль голоса").grid(row=0, column=0, columnspan=2, sticky="ew",
                                                     padx=18, pady=(16, 2))
        profrow = ctk.CTkFrame(tab_voice, fg_color=CARD2, corner_radius=12)
        profrow.grid(row=1, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 2),
                     ipadx=10, ipady=8)
        profrow.columnconfigure(0, weight=1)
        self.profile_menu = self._menu(profrow, self.profile_var, ["Без профиля"] + sorted(self.profiles))
        self.profile_menu.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(profrow, text="Сохранить", width=86, command=self._save_profile,
                      fg_color=FIELD, hover_color="#3a3a54", text_color=ACCENT,
                      corner_radius=10, font=(FONT, 12)).grid(row=0, column=1, padx=(8, 0))
        ctk.CTkButton(profrow, text="Удалить", width=72, command=self._delete_profile,
                      fg_color=FIELD, hover_color="#3a3a54", text_color=RED,
                      corner_radius=10, font=(FONT, 12)).grid(row=0, column=2, padx=(8, 0))
        # регистрируем обработчик после создания виджетов профиля, чтобы
        # программная установка self.profile_var выше не могла дёрнуть его раньше времени
        self.profile_var.trace_add("write", self._on_profile_change)

        self._cap(tab_voice, "Голос диктора").grid(row=2, column=0, sticky="ew", padx=(18, 9), pady=(14, 2))
        self._menu(tab_voice, self.voice_var, voice_names).grid(row=3, column=0, sticky="ew", padx=(18, 9))
        self._cap(tab_voice, "Точность (модель)").grid(row=2, column=1, sticky="ew", padx=(9, 18), pady=(14, 2))
        self._menu(tab_voice, self.model_var, MODELS).grid(row=3, column=1, sticky="ew", padx=(9, 18))

        self._cap(tab_voice, "Скорость речи").grid(row=4, column=0, sticky="ew", padx=(18, 9), pady=(14, 2))
        self._menu(tab_voice, self.speed_var, list(SPEEDS)).grid(row=5, column=0, sticky="ew", padx=(18, 9))
        self._cap(tab_voice, "Перевод (диктор на языке)").grid(row=4, column=1, sticky="ew", padx=(9, 18), pady=(14, 2))
        self._menu(tab_voice, self.lang_var, list(LANGUAGES)).grid(row=5, column=1, sticky="ew", padx=(9, 18))

        volcap = ctk.CTkFrame(tab_voice, fg_color="transparent")
        volcap.grid(row=6, column=0, columnspan=2, sticky="ew", padx=18, pady=(14, 2))
        ctk.CTkLabel(volcap, text="Громкость диктора", text_color=SUB, font=(FONT, 12)).pack(side="left")
        self.vol_lbl = ctk.CTkLabel(volcap, text=f"{self.vol_var.get()}%", text_color=ACCENT,
                                    fg_color=ACCENT_SOFT, corner_radius=8, width=48,
                                    font=(FONT, 12, "bold"))
        self.vol_lbl.pack(side="right")
        ctk.CTkSlider(tab_voice, from_=0, to=100, variable=self.vol_var, number_of_steps=100,
                      progress_color=ACCENT, button_color=ACCENT, button_hover_color=ACC_HOV,
                      fg_color=FIELD, command=self._on_vol).grid(row=7, column=0, columnspan=2, sticky="ew", padx=18)

        pitchcap = ctk.CTkFrame(tab_voice, fg_color="transparent")
        pitchcap.grid(row=8, column=0, columnspan=2, sticky="ew", padx=18, pady=(14, 2))
        ctk.CTkLabel(pitchcap, text="Тон голоса персонажа (только RVC)", text_color=SUB,
                     font=(FONT, 12)).pack(side="left")
        self.pitch_lbl = ctk.CTkLabel(pitchcap, text=self._pitch_text(pitch0), text_color=ACCENT,
                                      fg_color=ACCENT_SOFT, corner_radius=8, width=48,
                                      font=(FONT, 12, "bold"))
        self.pitch_lbl.pack(side="right")
        ctk.CTkSlider(tab_voice, from_=-12, to=12, variable=self.pitch_var, number_of_steps=24,
                      progress_color=ACCENT, button_color=ACCENT, button_hover_color=ACC_HOV,
                      fg_color=FIELD, command=self._on_pitch).grid(row=9, column=0, columnspan=2, sticky="ew",
                                                                    padx=18, pady=(0, 16))

        # --- вкладка «Устройства» ---
        # Микрофон — всегда системный по умолчанию (input_device_index=None).
        # Так распознавание гарантированно слушает тот же вход, что выбран в
        # Windows, без сопоставления индексов sounddevice<->PyAudio.
        self._cap(tab_dev, "🎤  Микрофон (системный по умолчанию)").grid(
            row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=(16, 2))

        microw = ctk.CTkFrame(tab_dev, fg_color="transparent")
        microw.grid(row=1, column=0, columnspan=2, sticky="ew", padx=18, pady=(2, 2))
        microw.columnconfigure(1, weight=1)
        ctk.CTkLabel(microw, text="Уровень", text_color=SUB, font=(FONT, 12)).grid(row=0, column=0, padx=(0, 10))
        self.mic_bar = ctk.CTkProgressBar(microw, progress_color=GREEN, fg_color=FIELD,
                                          height=12, corner_radius=6)
        self.mic_bar.grid(row=0, column=1, sticky="ew")
        self.mic_bar.set(0)

        self._cap(tab_dev, "🔈  Куда выводить звук").grid(row=2, column=0, columnspan=2, sticky="ew",
                                                       padx=18, pady=(14, 2))
        devrow = ctk.CTkFrame(tab_dev, fg_color="transparent")
        devrow.grid(row=3, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 16))
        devrow.columnconfigure(0, weight=1)
        self.device_menu = self._menu(devrow, self.device_var, devices)
        self.device_menu.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(devrow, text="↻", width=40, command=self.refresh_devices,
                      fg_color=FIELD, hover_color="#3a3a54", text_color=ACCENT,
                      corner_radius=10, font=(FONT, 15, "bold")).grid(row=0, column=1, padx=(8, 0))

        btns = ctk.CTkFrame(self.root, fg_color="transparent")
        btns.pack(pady=4)
        self.btn = ctk.CTkButton(btns, text="▶  Старт", command=self.toggle, width=200, height=46,
                                 fg_color=GREEN, hover_color=GREEN_H, text_color="#0c1410",
                                 corner_radius=18, font=(FONT, 15, "bold"))
        self.btn.pack(side="left", padx=5)
        self.mute_btn = ctk.CTkButton(btns, text="🔊", command=self.toggle_mute, width=58, height=46,
                                      fg_color=FIELD, hover_color="#3a3a54", text_color=TEXT,
                                      corner_radius=18, font=(FONT, 16))
        self.mute_btn.pack(side="left", padx=5)
        self.test_btn = ctk.CTkButton(btns, text="🔈 Тест", command=self.test, width=96, height=46,
                                      fg_color=FIELD, hover_color="#3a3a54", text_color=TEXT,
                                      corner_radius=18, font=(FONT, 13, "bold"))
        self.test_btn.pack(side="left", padx=5)
        self.hint_lbl = ctk.CTkLabel(self.root,
                     text="F8 — пауза звука   •   F9 — старт/стоп   •   крестик сворачивает в трей",
                     text_color=GREY, font=(FONT, 10))
        self.hint_lbl.pack(pady=(2, 2))

        self.topmost_var = ctk.BooleanVar(value=bool(self.cfg.get("topmost", False)))
        ctk.CTkSwitch(self.root, text="📌  Поверх всех окон", variable=self.topmost_var,
                      command=self._apply_topmost, progress_color=ACCENT,
                      text_color=SUB, font=(FONT, 11)).pack(pady=(0, 2))
        self._apply_topmost()

        inrow = ctk.CTkFrame(self.root, fg_color="transparent")
        inrow.pack(fill="x", padx=26, pady=(6, 2))
        inrow.columnconfigure(0, weight=1)
        self.text_entry = ctk.CTkEntry(inrow, placeholder_text="Введите текст и нажмите Enter — диктор озвучит…",
                                       fg_color=FIELD, text_color=TEXT, border_width=0,
                                       corner_radius=12, font=(FONT, 12), height=40)
        self.text_entry.grid(row=0, column=0, sticky="ew")
        self.text_entry.bind("<Return>", lambda e: self._say_typed())
        self.say_btn = ctk.CTkButton(inrow, text="📤 Озвучить", width=110, height=40, command=self._say_typed,
                                     fg_color=ACCENT, hover_color=ACC_HOV, text_color="#0c1410",
                                     corner_radius=12, font=(FONT, 12, "bold"))
        self.say_btn.grid(row=0, column=1, padx=(8, 0))

        labrow = ctk.CTkFrame(self.root, fg_color="transparent")
        labrow.pack(fill="x", padx=30, pady=(6, 3))
        ctk.CTkLabel(labrow, text="📝  РАСПОЗНАННАЯ РЕЧЬ", text_color=GREY,
                     font=(FONT, 10, "bold")).pack(side="left")
        ctk.CTkButton(labrow, text="Очистить", width=72, height=24, command=self._clear_log,
                      fg_color=FIELD, hover_color="#3a3a54", text_color=SUB,
                      corner_radius=8, font=(FONT, 10)).pack(side="right")
        self.log = ctk.CTkTextbox(self.root, fg_color=CARD, text_color=TEXT,
                                  font=("Consolas", 12), corner_radius=14, wrap="word")
        self.log.pack(fill="both", expand=True, padx=26, pady=(0, 14))

        # начальный снимок настроек для рабочих потоков (см. _sync_settings_snapshot)
        self._sync_settings_snapshot()

        if self._settings_load_error:
            self._log(self._settings_load_error)

        if not self._cable_found:
            self._log("VB-Cable не найден. Без него собеседники вас не услышат. "
                      "Установите его (https://vb-audio.com/Cable/), перезагрузите ПК "
                      "и нажмите ↻ рядом со списком устройств.")

    # ---------- devices ----------
    def _friendly(self, name):
        if "cable input" in name.lower():
            return "Виртуальный микрофон (CABLE Input)"
        if "(" in name and ")" not in name:
            name = name.split("(")[0].strip()
        return name

    def _query_devices(self, channel_key):
        """Общий опрос sd.query_devices() для устройств вывода/ввода.
        channel_key — 'max_output_channels' или 'max_input_channels'.
        Возвращает (labels, device_map); при сбое — (None, исключение)."""
        try:
            apis = [ha["name"] for ha in sd.query_hostapis()]
        except Exception:
            apis = []
        target = None
        for pref in ("Windows DirectSound", "Windows WASAPI", "MME"):
            if pref in apis:
                target = apis.index(pref); break
        try:
            devices = sd.query_devices()
        except Exception as e:
            return None, e
        labels = []
        device_map = {}
        for idx, dev in enumerate(devices):
            if dev[channel_key] <= 0:
                continue
            if target is not None and dev["hostapi"] != target:
                continue
            low = dev["name"].lower()
            if "sound mapper" in low or "primary sound" in low or "первичный" in low:
                continue
            label = self._friendly(dev["name"])
            base, n = label, 2
            while label in device_map:
                label = f"{base} ({n})"; n += 1
            device_map[label] = idx
            labels.append(label)
        return labels, device_map

    def _devices(self):
        labels, result = self._query_devices("max_output_channels")
        if labels is None:
            self._log(f"Не удалось получить список устройств вывода: {result}")
            return ["(нет устройств)"]
        self.device_map = result
        return labels or ["(нет устройств)"]

    def _cable_present(self, devices):
        """True, если среди устройств есть виртуальный микрофон VB-Cable."""
        return any("cable input" in d.lower() for d in devices)

    def _default_device(self, devices):
        for d in devices:
            if "cable input" in d.lower():
                return d
        return devices[0]

    def _map_idx(self, device_map, selected_label):
        if not device_map:
            return None
        if selected_label in device_map:
            return device_map[selected_label]
        # выбранная метка устарела (устройство пропало из списка) — берём
        # первое доступное вместо произвольного индекса 0, который может
        # не входить в device_map вовсе
        return next(iter(device_map.values()))

    def _device_idx(self):
        # читаем снимок, а не Tk-переменную: вызывается из рабочих потоков
        return self._map_idx(self.device_map, getattr(self, "_cur_output", self.device_var.get()))

    def refresh_devices(self):
        devices = self._devices()
        self.device_menu.configure(values=devices)
        if self.device_var.get() not in devices:
            self.device_var.set(self._default_device(devices))
        self._cable_found = self._cable_present(devices)
        if self._cable_found:
            self._log("Список устройств обновлён. Виртуальный микрофон (VB-Cable) найден.")
        else:
            self._log("Список устройств обновлён. VB-Cable пока не найден — "
                      "установите его и перезагрузите ПК: https://vb-audio.com/Cable/")

    def _on_setting_change(self, *args):
        self._save_settings()

    def _save_settings_soon(self):
        """Дебаунс записи настроек. Ползунок шлёт command на каждый шаг
        перетаскивания (десятки раз в секунду) — писать файл на каждый шаг
        накладно, поэтому откладываем запись на 400 мс после последнего
        изменения. Снимок _cur_* обновляем сразу (это дёшево и держит рабочие
        потоки в актуальном состоянии), а на диск сбрасываем с задержкой."""
        self._sync_settings_snapshot()
        if self._save_after_id is not None:
            try:
                self.root.after_cancel(self._save_after_id)
            except Exception:
                pass
        self._save_after_id = self.root.after(400, self._save_settings)

    def _ui(self, fn):
        """Безопасно планирует fn в главном потоке Tk. root.after() после
        root.destroy() (выход через трей) бросает TclError — глушим его, чтобы
        рабочий/тестовый поток не падал на финальной очистке."""
        try:
            self.root.after(0, fn)
        except Exception:
            pass

    def _on_vol(self, val):
        v = int(float(val))
        self.vol_lbl.configure(text=f"{v}%")
        if v == 0 and not self._vol_zero_warned:
            self._vol_zero_warned = True
            self._log("Громкость 0% — собеседники вас не услышат. Поднимите ползунок громкости.")
        elif v > 0:
            self._vol_zero_warned = False
        self._save_settings_soon()

    def _pitch_text(self, v):
        v = int(v)
        return f"{'+' if v > 0 else ''}{v}"

    def _on_pitch(self, val):
        self.pitch_lbl.configure(text=self._pitch_text(float(val)))
        self._save_settings_soon()

    # ---------- voice profiles ----------
    def _refresh_profile_menu(self):
        self.profile_menu.configure(values=["Без профиля"] + sorted(self.profiles))

    def _save_profile(self):
        dialog = ctk.CTkInputDialog(text="Имя профиля:", title="Сохранить профиль")
        name = dialog.get_input()
        if not name:
            return
        name = name.strip()
        if not name or name == "Без профиля":
            return
        self.profiles[name] = {
            "voice": self.voice_var.get(),
            "speed": self.speed_var.get(),
            "lang": self.lang_var.get(),
            "volume": int(self.vol_var.get()),
            "pitch": int(self.pitch_var.get()),
        }
        self._refresh_profile_menu()
        self.profile_var.set(name)
        self._save_settings()
        self._log(f"Профиль «{name}» сохранён.")

    def _delete_profile(self):
        name = self.profile_var.get()
        if name not in self.profiles:
            return
        del self.profiles[name]
        self._refresh_profile_menu()
        self.profile_var.set("Без профиля")
        self._save_settings()
        self._log(f"Профиль «{name}» удалён.")

    def _on_profile_change(self, *args):
        name = self.profile_var.get()
        if name != "Без профиля":
            self._apply_profile(name)

    def _apply_profile(self, name):
        p = self.profiles.get(name)
        if not isinstance(p, dict):
            return
        voice_pool = list(VOICES) + list(self.rvc_voices)
        if p.get("voice") in voice_pool:
            self.voice_var.set(p["voice"])
        if p.get("speed") in SPEEDS:
            self.speed_var.set(p["speed"])
        if p.get("lang") in LANGUAGES:
            self.lang_var.set(p["lang"])
        if "volume" in p:
            v = max(0, min(100, int(p["volume"])))
            self.vol_var.set(v)
            self._on_vol(v)
        if "pitch" in p:
            pt = max(-12, min(12, int(p["pitch"])))
            self.pitch_var.set(pt)
            self._on_pitch(pt)
        self._log(f"Профиль «{name}» применён.")

    def _apply_topmost(self):
        try:
            self.root.attributes("-topmost", bool(self.topmost_var.get()))
        except Exception:
            pass
        self._save_settings()

    def _clear_log(self):
        try:
            self.log.delete("1.0", "end")
        except Exception:
            pass

    # ---------- tray ----------
    def _setup_tray(self):
        try:
            import pystray
            from PIL import Image
            img = Image.open(_resource("icon.ico"))
            menu = pystray.Menu(
                pystray.MenuItem("Показать", lambda i, it: self.root.after(0, self._show)),
                pystray.MenuItem("Выход", lambda i, it: self.root.after(0, self._quit)),
            )
            self.tray = pystray.Icon("diktor", img, "Голос Диктора", menu)
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception as e:
            self.tray = None
            self._log(f"Значок в трее недоступен: {e}. Крестик будет закрывать программу.")
            # подсказка под кнопками обещает сворачивание в трей — раз трея нет,
            # честно правим её, чтобы пользователь не удивлялся закрытию по крестику
            try:
                self.hint_lbl.configure(
                    text="F8 — пауза звука   •   F9 — старт/стоп   •   крестик закрывает программу")
            except Exception:
                pass

    def _show(self):
        self.root.deiconify(); self.root.lift()

    def _hide_to_tray(self):
        if self.tray is not None:
            self.root.withdraw()
            return
        # трея нет — крестик закрывает программу. Если распознавание работает,
        # переспрашиваем, чтобы случайный клик не оборвал сессию посреди игры.
        if self.running:
            try:
                from tkinter import messagebox
                if not messagebox.askokcancel(
                        "Закрыть программу?",
                        "Распознавание сейчас работает. Закрыть программу и остановить его?"):
                    return
            except Exception:
                pass
        self._quit()

    def _quit(self):
        try:
            self._save_settings()
        except Exception:
            pass
        self.running = False
        self._discard_recorder()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        try:
            import keyboard
            keyboard.unhook_all()
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._stop_mic_monitor()
        self.root.destroy()

    # ---------- mic level ----------
    def _on_audio_chunk(self, chunk):
        """Считает уровень микрофона прямо из аудиопотока RealtimeSTT.
        Вызывается из его внутреннего потока на каждый кусок. Намеренно не
        открываем собственный sd.InputStream на тот же микрофон: два
        одновременных потока захвата на одном устройстве на Windows душат
        друг друга, и распознавание оставалось без звука."""
        try:
            data = np.frombuffer(chunk, dtype=np.int16)
            if data.size == 0:
                return
            data = data.astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(np.square(data))))
            # на пустом/битом куске mean даёт nan — не пускаем его в индикатор,
            # иначе полоска зависала на максимуме
            self.mic_level = rms if rms == rms else 0.0
        except Exception:
            pass

    def _stop_mic_monitor(self):
        self.mic_level = 0.0

    # ---------- hotkey ----------
    def _setup_hotkey(self):
        try:
            import keyboard
            keyboard.add_hotkey("f8", lambda: self.root.after(0, self.toggle_mute))
            keyboard.add_hotkey("f9", lambda: self.root.after(0, self.toggle))
            self._log("Горячие клавиши F8 (пауза) и F9 (старт/стоп) активны.")
        except Exception as e:
            self._log(f"Горячие клавиши не работают: {e}")
            self._log("Если не помогает — запустите Diktor.exe от имени администратора.")

    # ---------- ui queue ----------
    def _log(self, msg):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.ui_queue.put(("log", f"[{ts}] {msg}"))

    def _status(self, color, label):
        self.ui_queue.put(("status", (color, label)))

    def _drain(self):
        while not self.ui_queue.empty():
            kind, payload = self.ui_queue.get()
            if kind == "log":
                self.log.insert("end", payload + "\n"); self.log.see("end")
            else:
                color, label = payload
                self.dot.configure(text_color=color)
                self.status_lbl.configure(text=label, text_color=color)
        try:
            # после «Стоп» фоновый shutdown() рекордера ещё какое-то время шлёт
            # чанки в _on_audio_chunk — не даём полоске «жить», когда мы уже
            # не слушаем, иначе выглядит будто распознавание всё ещё идёт
            lvl = self.mic_level if self.running else 0.0
            # множитель 2.5 (а не 4): RMS речи в норме 0.05–0.3, при *4 полоска
            # почти всегда упиралась в максимум; nan/мусор -> 0
            level = 0.0 if lvl != lvl else min(1.0, max(0.0, lvl * 2.5))
            self.mic_bar.set(level)
            color = RED if level > 0.92 else YELLOW if level > 0.6 else GREEN
            self.mic_bar.configure(progress_color=color)
        except Exception:
            pass
        self.root.after(80, self._drain)

    def _force_redraw(self):
        try:
            self.root.update_idletasks()
        except Exception:
            pass

    def _on_root_configure(self, event):
        if event.widget is not self.root:
            return
        if self._redraw_after_id is not None:
            try:
                self.root.after_cancel(self._redraw_after_id)
            except Exception:
                pass
        # ждём, пока перетаскивание рамки/серия кликов по вкладкам утихнет,
        # и только потом форсируем перерисовку — иначе update_idletasks()
        # на каждое промежуточное событие <Configure> во время самого
        # перетаскивания будет только тормозить, а не помогать
        self._redraw_after_id = self.root.after(120, self._force_redraw)

    # ---------- voices (RVC) ----------
    def _scan_rvc_voices(self):
        """Ищет модели голосов (.pth) в папке voices/ рядом с программой."""
        found = {}
        try:
            if os.path.isdir(VOICES_DIR):
                for f in sorted(os.listdir(VOICES_DIR)):
                    if f.lower().endswith(".pth"):
                        stem = os.path.splitext(f)[0]
                        found[f"[Персонаж] {stem}"] = os.path.join(VOICES_DIR, f)
        except Exception:
            pass
        return found

    def _resolve_voice(self, display):
        """display -> (голос Edge TTS для синтеза, путь к RVC-модели или None)."""
        if display in self.rvc_voices:
            return RVC_BASE_VOICE, self.rvc_voices[display]
        if display not in VOICES:
            # выбранный голос пропал (например, удалили .pth персонажа при
            # открытой программе) — иначе пользователь молча получал чужой голос
            self._log(f"Голос «{display}» не найден — озвучиваю голосом по умолчанию.")
        return VOICES.get(display, list(VOICES.values())[0]), None

    def _find_index(self, pth_path):
        """Ищет .index рядом с .pth: сначала по совпадению имени, иначе любой."""
        try:
            d = os.path.dirname(pth_path)
            stem = os.path.splitext(os.path.basename(pth_path))[0]
            cands = [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(".index")]
            for c in cands:
                if stem.lower() in os.path.basename(c).lower():
                    return c
            if len(cands) > 1:
                self._log(f"RVC: для «{stem}» не нашёл подходящий .index по имени файла, "
                          f"беру первый попавшийся ({os.path.basename(cands[0])}) — "
                          f"переименуйте файлы, если он не тот.")
            return cands[0] if cands else None
        except Exception:
            return None

    def _ensure_rvc(self, model_path):
        """Лениво создаёт движок и (пере)загружает модель. Кэширует между фразами."""
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError("нужна видеокарта NVIDIA (CUDA недоступна)")
        from rvc_python.infer import RVCInference
        device = "cuda:0"
        if self._rvc is None:
            self._log("RVC: инициализация движка (первый запуск может качать базовые модели)...")
            self._rvc = RVCInference(device=device)
        if self._rvc_loaded_path != model_path:
            index = self._find_index(model_path)
            try:
                try:
                    self._rvc.load_model(model_path, index_path=index)
                except TypeError:
                    self._rvc.load_model(model_path)
                # параметры передаём максимально совместимо с разными версиями API
                for params in ({"f0method": "rmvpe", "index_path": index},
                               {"f0method": "rmvpe"}):
                    try:
                        self._rvc.set_params(**params)
                        break
                    except Exception:
                        continue
            except Exception:
                # неудачная загрузка может оставить движок в промежуточном
                # состоянии (ни старая, ни новая модель не загружены толком) —
                # сбрасываем кэш, чтобы следующий вызов (с любой моделью)
                # перезагружал с нуля, а не доверял старому _rvc_loaded_path
                self._rvc_loaded_path = None
                raise
            self._rvc_loaded_path = model_path
            self._log(f"RVC: модель загружена ({os.path.basename(model_path)}).")

    def _convert_rvc(self, samples, sr, model_path):
        """Накладывает тембр RVC-модели на синтезированный звук. При сбое — базовый голос."""
        if self._rvc_no_cuda:
            # без видеокарты NVIDIA конверсия невозможна в принципе — не пытаемся
            # и не засоряем лог повторными сообщениями об ошибке
            return samples, sr
        failed_at = self._rvc_failed_paths.get(model_path)
        if failed_at is not None and (time.monotonic() - failed_at) < RVC_RETRY_COOLDOWN:
            # недавно сбоила — временно играем базовым голосом, не дёргая RVC,
            # но по истечении кулдауна попробуем снова
            return samples, sr
        import tempfile
        with self._rvc_lock:
            in_path = out_path = None
            try:
                self._ensure_rvc(model_path)
                try:
                    self._rvc.set_params(f0up_key=self._cur_pitch)
                except Exception:
                    pass
                fd_in, in_path = tempfile.mkstemp(suffix=".wav"); os.close(fd_in)
                fd_out, out_path = tempfile.mkstemp(suffix=".wav"); os.close(fd_out)
                sf.write(in_path, samples, sr)
                self._rvc.infer_file(in_path, out_path)
                out, out_sr = sf.read(out_path, dtype="float32")
                self._rvc_failed_paths.pop(model_path, None)
                return out, out_sr
            except Exception as e:
                # если дело в отсутствии NVIDIA-видеокарты — это не временный сбой,
                # а принципиальная невозможность: отключаем RVC до перезапуска и
                # даём одно понятное сообщение вместо ретраев каждые 60 с
                try:
                    import torch
                    if not torch.cuda.is_available():
                        self._rvc_no_cuda = True
                except Exception:
                    pass
                if self._rvc_no_cuda:
                    self._log("Голоса персонажей (RVC) работают только с видеокартой NVIDIA — "
                              "у вас её нет. Озвучиваю обычным голосом диктора.")
                else:
                    self._rvc_failed_paths[model_path] = time.monotonic()
                    self._log(f"Голос персонажа «{os.path.basename(model_path)}» не сработал ({e}). "
                              f"Играю обычным голосом; повторю через {RVC_RETRY_COOLDOWN} с. "
                              f"Возможно, файл .pth повреждён или несовместим.")
                return samples, sr
            finally:
                for p in (in_path, out_path):
                    if p:
                        try:
                            os.remove(p)
                        except Exception:
                            pass

    # ---------- audio / translate ----------
    def _translate(self, text, target):
        # Перевод гоняем в daemon-потоке: deep_translator/requests могут зависнуть
        # на плохой сети без таймаута, а daemon-поток не помешает закрыть программу.
        result = {}
        done = threading.Event()

        def do():
            try:
                from deep_translator import GoogleTranslator
                result["text"] = GoogleTranslator(source="ru", target=target).translate(text)
            except Exception as e:
                result["error"] = e
            finally:
                done.set()

        threading.Thread(target=do, daemon=True).start()
        if not done.wait(timeout=10):
            self._log("Перевод: нет ответа от сервера (таймаут 10 с)")
            return None
        if "error" in result:
            self._log(f"Перевод недоступен: {result['error']}")
            return None
        return result.get("text")

    def _synth(self, text, voice, rate):
        async def go():
            comm = edge_tts.Communicate(text, voice, rate=rate)
            data = b""
            async for ch in comm.stream():
                if ch["type"] == "audio":
                    data += ch["data"]
            return data
        fut = asyncio.run_coroutine_threadsafe(go(), self._loop)
        try:
            mp3 = fut.result(timeout=15)
        except Exception as e:
            # отменяем корутину, иначе при таймауте она продолжает висеть в фоновом
            # event-loop и копить данные (утечка при череде плохих ответов сети)
            fut.cancel()
            self._log(f"Озвучка недоступна: {e}")
            return None, None
        if not mp3:
            self._log("Озвучка недоступна: сервер не вернул звук.")
            return None, None
        try:
            return sf.read(io.BytesIO(mp3), dtype="float32")
        except Exception as e:
            self._log(f"Озвучка недоступна: не удалось декодировать звук ({e}).")
            return None, None

    def _play(self, samples, sr, device_idx):
        if device_idx is None:
            self._log("Нет доступных устройств вывода — воспроизведение пропущено.")
            return
        v = getattr(self, "_cur_vol", 100) / 100.0
        try:
            sd.stop()
            sd.play(np.asarray(samples) * v, sr, device=device_idx)
            sd.wait()
        except Exception as e:
            self._log(f"Ошибка воспроизведения: {e}")

    def _resolve_translation(self, text, lang_disp, edge_voice):
        """Если для lang_disp задан перевод — переводит text и возвращает
        (перевод, голос перевода); иначе (text, edge_voice) без изменений.
        Если перевод запрошен, но недоступен (сбой сети/таймаут) — откатывается
        на озвучку оригинала по-русски, чтобы фраза не пропала совсем."""
        tcode, tvoice = LANGUAGES.get(lang_disp, (None, None))
        if not tcode:
            return text, edge_voice
        out = self._translate(text, tcode)
        if not out:
            # перевод не удался (нет сети/таймаут) — НЕ роняем фразу молча, иначе
            # собеседники слышат тишину, хотя пользователь говорил; озвучиваем
            # оригинал по-русски, чтобы его всё-таки было слышно
            self._log("Перевод недоступен — озвучиваю оригинал по-русски.")
            return text, edge_voice
        self._log(f"→ {out}")
        return out, tvoice

    def _synth_convert_play(self, text, voice, rate, rvc_path, respect_state=False):
        """Синтез -> (опционально) RVC-конверсия -> проигрывание. Общий конвейер
        для теста, озвучки набранного текста и основного цикла распознавания.
        Захватывает _speak_lock на весь конвейер, а не только на проигрывание:
        иначе тест/набранный текст/распознанная речь могли синтезироваться
        параллельно и выходить в динамики практически одновременно.

        respect_state=True (путь распознавания): пока шли синтез/RVC — это
        секунды — пользователь мог нажать паузу (F8) или «Стоп»; в этом случае
        уже неактуальную фразу в микрофон не выпускаем. Для «Теста» и озвучки
        набранного текста (явные действия) флаг False — они играют всегда."""
        with self._speak_lock:
            s, sr = self._synth(text, voice, rate)
            if s is not None and rvc_path:
                s, sr = self._convert_rvc(s, sr, rvc_path)
            if s is None:
                return
            if respect_state and (self.muted or not self.running):
                return
            # индекс устройства берём прямо перед игрой звука, а не заранее:
            # синтез/RVC может занять много секунд, а пользователь — успеть
            # обновить список устройств (↻) за это время
            self._play(s, sr, self._device_idx())

    # ---------- control ----------
    def toggle(self):
        self.stop() if self.running else self.start()

    def _request_recorder_restart(self, reason_msg):
        """Просит рабочий поток пересобрать рекордер свежими настройками
        (модель/устройство ввода), завершив текущий .text(). Используется
        и при смене модели, и при смене микрофона."""
        if not self.running:
            return
        # Забираем self.recorder и сразу сбрасываем в None под тем же замком,
        # что и _install_recorder/_discard_recorder — атомарно, как и они:
        # 1) если «Стоп»/перезапуск из-за серии ошибок уже забрали рекордер
        #    себе, здесь окажется None — запрос просто молча отменяется, а не
        #    выключает один и тот же рекордер второй раз (на этот случай уже
        #    напоролись: второй .shutdown() из stop() мог зависнуть);
        # 2) рабочий цикл, увидев self.recorder == None, не сможет случайно
        #    дёрнуть .text() на уже выключаемом объекте на следующей итерации.
        with self._recorder_lock:
            rec, self.recorder = self.recorder, None
        # помечаем перезапуск ВСЕГДА, даже если rec уже None (идёт предыдущая
        # пересборка): рабочий цикл увидит флаг и пересоберёт рекордер ещё раз
        # уже с самыми свежими настройками. Иначе быстрая повторная смена модели
        # (medium → сразу tiny, пока medium ещё грузится) терялась, и рекордер
        # оставался на промежуточной модели, хотя список показывал уже другую.
        self._recorder_restart_pending = True
        self._log(reason_msg)
        self._status(YELLOW, "Перезапуск")
        if rec is None:
            return

        def shutdown():
            try:
                rec.shutdown()
            except Exception:
                pass
        # rec.shutdown() вызывается из трассировки model_var,
        # которая срабатывает прямо на главном потоке Tk (нажатие в выпадающем
        # списке, выбор профиля) — если shutdown() на секунду заблокируется
        # (например, ждёт внутренний поток рекордера, который как раз сейчас
        # отдаёт результат .text() рабочему потоку), всё окно зависает. Гоним
        # его в отдельном потоке, чтобы интерфейс не подвисал ни при каких
        # раскладах.
        t = threading.Thread(target=shutdown, daemon=True)
        # рабочий поток дождётся этого потока перед make_recorder() —
        # см. _join_recorder_shutdown(). Без этого быстрая смена модели/микрофона
        # (или серия кликов по вкладкам) могла запустить сборку нового
        # рекордера, пока старый ещё не отпустил аудиоустройство и внутренние
        # потоки — два AudioToTextRecorder одновременно дерутся за один и тот
        # же микрофон/CPU, что и давало зависание и ошибку загрузки модели.
        with self._recorder_lock:
            self._recorder_shutdown_threads.append(t)
        t.start()

    def _on_model_change(self, *args):
        self._save_settings()
        self._request_recorder_restart(f"Меняю модель распознавания на «{self.model_var.get()}»...")

    def toggle_mute(self):
        self.muted = not self.muted
        if self.muted:
            self.mute_btn.configure(text="🔇", fg_color="#3a2030")
            # глушим уже играющую фразу сразу, а не только следующую —
            # F8 для пользователя это «тишина сейчас»
            try:
                sd.stop()
            except Exception:
                pass
            if self.running:
                self._status(YELLOW, "Пауза")
        else:
            self.mute_btn.configure(text="🔊", fg_color=FIELD)
            self._status(ACCENT, "Прослушивание") if self.running else self._status(GREY, "Остановлено")

    def test(self):
        if self._testing:
            return
        self._testing = True
        self.test_btn.configure(state="disabled")
        self._save_settings()
        edge_voice, rvc_path = self._resolve_voice(self.voice_var.get())
        # «Тест» — явное действие пользователя, поэтому даём голосу RVC ещё один
        # шанс, даже если предыдущая попытка попала в чёрный список (например,
        # из-за разового сбоя сети при скачивании базовых моделей).
        if rvc_path:
            self._rvc_failed_paths.pop(rvc_path, None)
        rate = SPEEDS[self.speed_var.get()]
        self._log("Тест: воспроизвожу проверочную фразу...")
        if LANGUAGES.get(self.lang_var.get(), (None, None))[0]:
            self._log("Тест озвучивает проверочную фразу по-русски; перевод применяется "
                      "к распознанной и набранной речи, но не к самой тестовой фразе.")
        if "cable input" in self.device_var.get().lower():
            self._log("Звук идёт в виртуальный микрофон — в самой программе вы его не услышите, "
                      "это нормально (его слышат собеседники). Чтобы проверить на слух, "
                      "временно выберите в списке свои динамики или наушники.")

        def run():
            try:
                self._synth_convert_play(TEST_PHRASE, edge_voice, rate, rvc_path)
                self._log("Тест завершён.")
            except Exception as e:
                self._log(f"Ошибка теста: {e}")
            finally:
                self._testing = False
                self._ui(lambda: self.test_btn.configure(state="normal"))
        threading.Thread(target=run, daemon=True).start()

    def _say_typed(self):
        if self._saying:
            # уже озвучиваем предыдущий текст — не копим очередь фраз, пока
            # пользователь нетерпеливо жмёт Enter/«Озвучить» несколько раз
            return
        text = self.text_entry.get().strip()
        if not text:
            return
        self._saying = True
        self.say_btn.configure(state="disabled")
        try:
            self.text_entry.delete(0, "end")
            self._log(f"⌨ {text}")
            edge_voice, rvc_path = self._resolve_voice(self.voice_var.get())
            lang_disp = self.lang_var.get()
            rate = SPEEDS[self.speed_var.get()]
        except Exception as e:
            # если что-то упало ДО запуска потока — вернуть кнопку в рабочее
            # состояние, иначе она осталась бы навсегда заблокированной
            self._saying = False
            self.say_btn.configure(state="normal")
            self._log(f"Ошибка озвучки текста: {e}")
            return

        def run():
            try:
                resolved = self._resolve_translation(text, lang_disp, edge_voice)
                if resolved is None:
                    return
                out, out_voice = resolved
                self._synth_convert_play(out, out_voice, rate, rvc_path)
            except Exception as e:
                self._log(f"Ошибка озвучки текста: {e}")
            finally:
                self._saying = False
                self._ui(lambda: self.say_btn.configure(state="normal"))
        threading.Thread(target=run, daemon=True).start()

    def start(self):
        self.running = True
        self.btn.configure(text="■  Стоп", fg_color=RED, hover_color=RED_H)
        self._status(YELLOW, "Загрузка")
        self._save_settings()
        threading.Thread(target=self._run, daemon=True).start()

    def _discard_recorder(self):
        # _recorder_lock делает чтение-и-сброс self.recorder атомарным относительно
        # рабочего потока (_run), который под тем же замком решает, можно ли
        # установить новый рекордер после перезапуска модели/распознавания —
        # без этого окно между "self.running ещё True" и "self.recorder = new"
        # позволяет новому рекордеру пережить stop()/_quit() и зависнуть без владельца.
        with self._recorder_lock:
            rec, self.recorder = self.recorder, None
        if rec is None:
            return
        # stop()/_quit() вызывают это прямо на главном потоке Tk (нажатие кнопки,
        # пункт меню трея). rec.shutdown() не гарантированно быстрый — если он
        # зависнет (например, рекордер как раз отдаёт результат .text() рабочему
        # потоку), всё окно подвиснет вместе с ним. Гоним в отдельный поток, как
        # и в _request_recorder_restart.
        def shutdown():
            try:
                rec.shutdown()
            except Exception:
                pass
        t = threading.Thread(target=shutdown, daemon=True)
        # см. комментарий в _request_recorder_restart: следующий make_recorder()
        # (если пользователь сразу нажмёт «Старт» заново) дождётся этого потока
        with self._recorder_lock:
            self._recorder_shutdown_threads.append(t)
        t.start()

    def _join_recorder_shutdown(self):
        """Дожидается завершения ВСЕХ отложенных rec.shutdown(), запущенных в
        фоне _discard_recorder()/_request_recorder_restart(), прежде чем строить
        новый рекордер. AudioToTextRecorder.shutdown() не гарантирует, что
        устройство ввода и внутренние потоки уже освобождены к моменту, когда
        .text() вышел из блокировки — без этого ожидания make_recorder() мог
        начать открывать тот же микрофон и грузить модель, пока старый рекордер
        ещё не отпустил ресурсы, и оба боролись за один и тот же микрофон/CPU
        (это и давало зависание при частых кликах по вкладкам и сбой загрузки
        модели). Список читаем-и-чистим под замком в цикле, чтобы не пропустить
        поток, добавленный главным потоком между итерациями. Вызывается только
        из рабочего потока, поэтому ожидание здесь не блокирует интерфейс."""
        while True:
            with self._recorder_lock:
                pending = self._recorder_shutdown_threads
                self._recorder_shutdown_threads = []
            if not pending:
                return
            for t in pending:
                t.join()

    def _install_recorder(self, new_recorder):
        """Атомарно ставит new_recorder как текущий рекордер, если приложение
        всё ещё запущено; иначе выключает его. Без общего замка с
        _discard_recorder() здесь была гонка: между проверкой self.running
        и записью self.recorder мог отработать stop()/_quit() из главного
        потока, после чего новый рекордер всё равно подменял бы self.recorder
        и оставался бы без владельца до конца процесса."""
        with self._recorder_lock:
            if not self.running:
                stale = new_recorder
            else:
                self.recorder = new_recorder
                stale = None
        if stale is None:
            return True
        try:
            stale.shutdown()
        except Exception:
            pass
        return False

    def _reassert_status(self):
        """Повторно выставляет «финальный» статус (Остановлено/Ошибка) после
        того, как очередь UI успела опустеть. Рабочий поток мог поставить в
        очередь «Прослушивание»/«Озвучивание» за мгновение до того, как заметил
        running=False — без переустановки индикатор завис бы на нём, хотя
        кнопка уже «Старт». Срабатывает только если мы всё ещё остановлены."""
        if self.running or not self._desired_status:
            return
        color, label = self._desired_status
        try:
            self.dot.configure(text_color=color)
            self.status_lbl.configure(text=label, text_color=color)
        except Exception:
            pass

    def stop(self):
        self.running = False
        self._stop_mic_monitor()
        self.btn.configure(text="▶  Старт", fg_color=GREEN, hover_color=GREEN_H)
        self._status(GREY, "Остановлено")
        self._desired_status = (GREY, "Остановлено")
        self.root.after(160, self._reassert_status)
        self._log("Остановка.")
        self._discard_recorder()

    def _abort_run(self, recorder_dead=False):
        """Аварийно завершает рабочий поток при ошибке: сбрасывает интерфейс
        в исходное состояние, но оставляет статус «Ошибка» (в отличие от
        обычной остановки). Вызывается из рабочего потока."""
        self.running = False
        if recorder_dead:
            # рекордер уже выключен в ветке ошибки — не выключать его повторно;
            # сбрасываем под тем же замком, что и _install_recorder/_discard_recorder
            with self._recorder_lock:
                self.recorder = None
        self._ui(self.stop)
        self._ui(lambda: self._status(RED, "Ошибка"))
        # переустанавливаем именно «Ошибку» (а не «Остановлено» от stop())
        self._ui(lambda: setattr(self, "_desired_status", (RED, "Ошибка")))

    # ---------- worker ----------
    def _run(self):
        try:
            import torch
            from RealtimeSTT import AudioToTextRecorder
        except Exception as e:
            self._log(f"Не установлены библиотеки: {e}")
            self._abort_run()
            return

        device = "cuda" if torch.cuda.is_available() else "cpu"
        # float32 на обоих устройствах -> одинаковая точность распознавания
        compute = "float32"
        self._log(f"Устройство: {'видеокарта (cuda)' if device=='cuda' else 'процессор (cpu)'}")
        if device == "cpu" and self._cur_model in ("small", "medium"):
            self._log(f"Внимание: модель «{self._cur_model}» без видеокарты работает медленно — "
                      "распознавание будет с заметной задержкой. Для быстрой работы выберите "
                      "«tiny» или «base».")
        self._log("Загрузка модели (при первом запуске — загрузка из интернета)...")

        def make_recorder():
            model = self._cur_model
            beam = MODEL_BEAM.get(model, 5)
            self._log(f"Модель: {model}  |  точность: float32  |  beam: {beam}")
            kw = dict(
                model=model, language="ru", spinner=False,
                device=device, compute_type=compute,
                post_speech_silence_duration=0.4,
                beam_size=beam,
                initial_prompt=WHISPER_PROMPT,
                input_device_index=None,
                # индикатор уровня микрофона питаем из того же потока, что и
                # распознавание — БЕЗ отдельного sd.InputStream на тот же
                # микрофон. Раньше второй поток на то же устройство душил поток
                # RealtimeSTT (звук шёл в индикатор, а распознавание получало
                # тишину — отсюда «звук есть, а слов нет»).
                on_recorded_chunk=self._on_audio_chunk,
            )
            try:
                return AudioToTextRecorder(**kw)
            except TypeError:
                # на случай версии RealtimeSTT без on_recorded_chunk —
                # лучше работающее распознавание без индикатора, чем падение
                kw.pop("on_recorded_chunk", None)
                return AudioToTextRecorder(**kw)

        # если пользователь только что нажал «Стоп» и сразу «Старт», shutdown()
        # предыдущего рекордера может ещё выполняться в фоне — дожидаемся его,
        # иначе новый AudioToTextRecorder откроет тот же микрофон, пока старый
        # ещё не отпустил его
        self._join_recorder_shutdown()
        try:
            new_recorder = make_recorder()
        except Exception as e:
            self._log(f"Ошибка запуска распознавания: {e}")
            self._log("Если это первый запуск — модель скачивается из интернета. Проверьте "
                      "подключение к сети и нажмите «Старт» ещё раз.")
            self._abort_run()
            return
        if not self._install_recorder(new_recorder):
            # пользователь успел нажать «Стоп», пока грузилась модель (загрузка
            # может занять десятки секунд, особенно при первой загрузке из
            # интернета) — stop() уже сбросил интерфейс, новый рекордер выключен
            # внутри _install_recorder, тут просто выходим без рабочего цикла
            return

        self._log("Готово к работе.")
        self._status(ACCENT, "Прослушивание") if not self.muted else self._status(YELLOW, "Пауза")
        last_text = ""
        last_text_time = 0.0
        errors = 0

        def restart_recorder(success_msg):
            """Пересобирает рекордер свежими настройками. Возвращает False,
            если рабочий цикл должен сразу завершиться (стоп во время
            пересборки или ошибка самой пересборки) — иначе True."""
            nonlocal errors, last_text, last_text_time
            try:
                # см. комментарий в _join_recorder_shutdown(): дожидаемся, пока
                # старый рекордер (выключенный в фоне при смене модели/микрофона)
                # действительно отпустит микрофон, прежде чем открывать новый
                self._join_recorder_shutdown()
                new_recorder = make_recorder()
                if not self._install_recorder(new_recorder):
                    return False
                errors = 0
                last_text = ""
                last_text_time = 0.0
                self._log(success_msg)
                if self.running and not self.muted:
                    self._status(ACCENT, "Прослушивание")
                return True
            except Exception as e2:
                self._log(f"Не удалось перезапустить распознавание: {e2}")
                self._abort_run(recorder_dead=True)
                return False

        def call_text_async(rec):
            """Запускает rec.text() в отдельном потоке вместо того, чтобы ждать
            его прямо в рабочем цикле. AudioToTextRecorder.shutdown() старого
            рекордера не гарантированно прерывает уже идущий блокирующий вызов
            .text() — на практике он мог просто продолжать ждать речь сколько
            угодно, и смена модели/микрофона повисала до тех пор, пока
            пользователь не нажимал «Стоп», а потом «Старт» заново. Цикл ниже
            ждёт результат с таймаутом и параллельно проверяет
            _recorder_restart_pending, поэтому может пересобрать рекордер,
            не дожидаясь возврата из старого (всё ещё повисшего) вызова —
            тот сам завершится позже сам по себе и его результат будет
            просто отброшен."""
            call = {"event": threading.Event(), "text": None, "exc": None}
            def run():
                try:
                    call["text"] = rec.text()
                except Exception as e:
                    call["exc"] = e
                finally:
                    call["event"].set()
            threading.Thread(target=run, daemon=True).start()
            return call

        pending_call = None
        while self.running:
            if pending_call is None:
                rec = self.recorder
                if rec is None:
                    # рекордер уже забран на пересборку (см. _request_recorder_restart),
                    # но новый ещё не установлен — короткими порциями ждём,
                    # не блокируясь насмерть
                    if self._recorder_restart_pending:
                        self._recorder_restart_pending = False
                        if not restart_recorder("Распознавание перезапущено с новыми настройками."):
                            break
                    else:
                        threading.Event().wait(0.05)
                    continue
                pending_call = call_text_async(rec)

            finished = pending_call["event"].wait(timeout=0.15)

            if not self.running:
                break

            # Проверяем флаг здесь, а не только после завершения pending_call:
            # это и даёт мгновенную смену модели/микрофона, не дожидаясь, пока
            # (возможно, навсегда повисший) .text() старого рекордера вернётся.
            if self._recorder_restart_pending:
                self._recorder_restart_pending = False
                pending_call = None
                if not restart_recorder("Распознавание перезапущено с новыми настройками."):
                    break
                continue

            if not finished:
                continue

            text, exc = pending_call["text"], pending_call["exc"]
            pending_call = None

            if exc is not None:
                self._log(f"Пропущено: {exc}")
                errors += 1
                if errors >= 5 and self.running:
                    self._log("Слишком много ошибок подряд — перезапуск распознавания...")
                    self._status(YELLOW, "Перезапуск")
                    with self._recorder_lock:
                        old_recorder, self.recorder = self.recorder, None
                    if old_recorder is not None:
                        try:
                            old_recorder.shutdown()
                        except Exception:
                            pass
                    if not restart_recorder("Распознавание перезапущено."):
                        break
                continue

            errors = 0
            text = (text or "").strip()
            if len(text) < 2:
                continue
            # дубликат подавляем только если он пришёл почти сразу за прошлым
            # (RealtimeSTT иногда отдаёт ту же фразу дважды). Если пользователь
            # сознательно повторил короткую фразу («Да», «Да») спустя секунды —
            # озвучиваем, иначе казалось, что программа «проглатывает» повторы
            now = time.monotonic()
            if text == last_text and (now - last_text_time) < 3.0:
                continue
            last_text = text
            last_text_time = now
            self._log(f"› {text}")
            if self.muted:
                continue

            rate = SPEEDS.get(self._cur_speed, list(SPEEDS.values())[0])
            edge_voice, rvc_path = self._resolve_voice(self._cur_voice)
            resolved = self._resolve_translation(text, self._cur_lang, edge_voice)
            if resolved is None:
                continue
            out, out_voice = resolved

            self._status(GREEN, "Озвучивание")
            self._synth_convert_play(out, out_voice, rate, rvc_path, respect_state=True)
            if self.running and not self.muted:
                self._status(ACCENT, "Прослушивание")


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    try:
        root.iconbitmap(_resource("icon.ico"))
    except Exception:
        pass
    DiktorApp(root)
    root.mainloop()
