import io
import os
import sys
import json
import asyncio
import datetime
import threading
import queue

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
BG="#15151f"; CARD="#1f1f2e"; FIELD="#2a2a3c"; TEXT="#e6e9f0"; SUB="#9aa0b4"
ACCENT="#7c8cff"; ACC_HOV="#6675f0"; GREEN="#3ecf8e"; GREEN_H="#34b87d"
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


class DiktorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Голос Диктора")
        self.root.geometry("560x970")
        self.root.minsize(520, 920)
        self.root.configure(fg_color=BG)

        self.running = False
        self.muted = False
        self.recorder = None
        self._recorder_lock = threading.Lock()
        self._recorder_restart_pending = False
        self.device_map = {}
        self.input_device_map = {}
        self._play_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        self._testing = False
        self.tray = None
        self.ui_queue = queue.Queue()
        self.mic_level = 0.0
        self._mic_stream = None
        self._rvc = None
        self._rvc_loaded_path = None
        self._rvc_failed_paths = set()
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

    # ---------- settings ----------
    def _load_settings(self):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_settings(self):
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "voice": self.voice_var.get(),
                    "model": self.model_var.get(),
                    "device": self.device_var.get(),
                    "input_device": self.input_device_var.get(),
                    "speed": self.speed_var.get(),
                    "lang": self.lang_var.get(),
                    "volume": int(self.vol_var.get()),
                    "pitch": int(self.pitch_var.get()),
                    "topmost": bool(self.topmost_var.get()),
                    "profiles": self.profiles,
                }, f, ensure_ascii=False)
        except Exception:
            pass

    # ---------- ui ----------
    def _cap(self, parent, text):
        return ctk.CTkLabel(parent, text=text, text_color=SUB, font=(FONT, 12), anchor="w")

    def _menu(self, parent, variable, values, **kw):
        return ctk.CTkOptionMenu(parent, variable=variable, values=values,
                                 fg_color=FIELD, button_color=FIELD,
                                 button_hover_color="#34344a", text_color=TEXT,
                                 dropdown_fg_color=CARD, dropdown_hover_color=FIELD,
                                 dropdown_text_color=TEXT, corner_radius=10,
                                 font=(FONT, 12), dropdown_font=(FONT, 12), **kw)

    def _build(self):
        head = ctk.CTkFrame(self.root, fg_color="transparent")
        head.pack(fill="x", padx=26, pady=(20, 2))
        ctk.CTkLabel(head, text="Голос Диктора", text_color=TEXT,
                     font=(FONT, 24, "bold")).pack(side="left")
        self.dot = ctk.CTkLabel(head, text="●", text_color=GREY, font=(FONT, 14))
        self.dot.pack(side="right", padx=(6, 0))
        self.status_lbl = ctk.CTkLabel(head, text="Остановлено", text_color=SUB, font=(FONT, 12))
        self.status_lbl.pack(side="right")

        ctk.CTkLabel(self.root, text="После паузы в речи программа озвучит сказанное голосом диктора",
                     text_color=SUB, font=(FONT, 11), anchor="w").pack(fill="x", padx=28)

        tabs = ctk.CTkTabview(self.root, fg_color=CARD, corner_radius=16,
                              segmented_button_fg_color=FIELD,
                              segmented_button_selected_color=ACCENT,
                              segmented_button_selected_hover_color=ACC_HOV,
                              segmented_button_unselected_hover_color="#34344a",
                              text_color=TEXT)
        tabs.pack(fill="x", padx=26, pady=14)
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

        in_devices = self._input_devices()
        indev0 = (self.cfg.get("input_device") if self.cfg.get("input_device") in in_devices
                  else self._default_input_device(in_devices))
        self.input_device_var = ctk.StringVar(value=indev0)
        self.input_device_var.trace_add("write", self._on_input_device_change)

        self.profile_var = ctk.StringVar(value="Без профиля")

        # --- вкладка «Голос» ---
        self._cap(tab_voice, "Профиль голоса").grid(row=0, column=0, columnspan=2, sticky="ew",
                                                     padx=18, pady=(16, 2))
        profrow = ctk.CTkFrame(tab_voice, fg_color="transparent")
        profrow.grid(row=1, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 2))
        profrow.columnconfigure(0, weight=1)
        self.profile_menu = self._menu(profrow, self.profile_var, ["Без профиля"] + sorted(self.profiles))
        self.profile_menu.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(profrow, text="Сохранить", width=86, command=self._save_profile,
                      fg_color=FIELD, hover_color="#34344a", text_color=ACCENT,
                      corner_radius=10, font=(FONT, 12)).grid(row=0, column=1, padx=(8, 0))
        ctk.CTkButton(profrow, text="Удалить", width=72, command=self._delete_profile,
                      fg_color=FIELD, hover_color="#34344a", text_color=RED,
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
        self.vol_lbl = ctk.CTkLabel(volcap, text=f"{self.vol_var.get()}%", text_color=ACCENT, font=(FONT, 12))
        self.vol_lbl.pack(side="right")
        ctk.CTkSlider(tab_voice, from_=0, to=100, variable=self.vol_var, number_of_steps=100,
                      progress_color=ACCENT, button_color=ACCENT, button_hover_color=ACC_HOV,
                      fg_color=FIELD, command=self._on_vol).grid(row=7, column=0, columnspan=2, sticky="ew", padx=18)

        pitchcap = ctk.CTkFrame(tab_voice, fg_color="transparent")
        pitchcap.grid(row=8, column=0, columnspan=2, sticky="ew", padx=18, pady=(14, 2))
        ctk.CTkLabel(pitchcap, text="Тон голоса персонажа (только RVC)", text_color=SUB,
                     font=(FONT, 12)).pack(side="left")
        self.pitch_lbl = ctk.CTkLabel(pitchcap, text=self._pitch_text(pitch0), text_color=ACCENT, font=(FONT, 12))
        self.pitch_lbl.pack(side="right")
        ctk.CTkSlider(tab_voice, from_=-12, to=12, variable=self.pitch_var, number_of_steps=24,
                      progress_color=ACCENT, button_color=ACCENT, button_hover_color=ACC_HOV,
                      fg_color=FIELD, command=self._on_pitch).grid(row=9, column=0, columnspan=2, sticky="ew",
                                                                    padx=18, pady=(0, 16))

        # --- вкладка «Устройства» ---
        self._cap(tab_dev, "Микрофон (вход)").grid(row=0, column=0, columnspan=2, sticky="ew",
                                                    padx=18, pady=(16, 2))
        inrow_dev = ctk.CTkFrame(tab_dev, fg_color="transparent")
        inrow_dev.grid(row=1, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 2))
        inrow_dev.columnconfigure(0, weight=1)
        self.input_device_menu = self._menu(inrow_dev, self.input_device_var, in_devices)
        self.input_device_menu.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(inrow_dev, text="↻", width=40, command=self.refresh_input_devices,
                      fg_color=FIELD, hover_color="#34344a", text_color=ACCENT,
                      corner_radius=10, font=(FONT, 15, "bold")).grid(row=0, column=1, padx=(8, 0))

        microw = ctk.CTkFrame(tab_dev, fg_color="transparent")
        microw.grid(row=2, column=0, columnspan=2, sticky="ew", padx=18, pady=(10, 2))
        microw.columnconfigure(1, weight=1)
        ctk.CTkLabel(microw, text="Уровень", text_color=SUB, font=(FONT, 12)).grid(row=0, column=0, padx=(0, 10))
        self.mic_bar = ctk.CTkProgressBar(microw, progress_color=GREEN, fg_color=FIELD,
                                          height=12, corner_radius=6)
        self.mic_bar.grid(row=0, column=1, sticky="ew")
        self.mic_bar.set(0)

        self._cap(tab_dev, "Куда выводить звук").grid(row=3, column=0, columnspan=2, sticky="ew",
                                                       padx=18, pady=(14, 2))
        devrow = ctk.CTkFrame(tab_dev, fg_color="transparent")
        devrow.grid(row=4, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 16))
        devrow.columnconfigure(0, weight=1)
        self.device_menu = self._menu(devrow, self.device_var, devices)
        self.device_menu.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(devrow, text="↻", width=40, command=self.refresh_devices,
                      fg_color=FIELD, hover_color="#34344a", text_color=ACCENT,
                      corner_radius=10, font=(FONT, 15, "bold")).grid(row=0, column=1, padx=(8, 0))

        btns = ctk.CTkFrame(self.root, fg_color="transparent")
        btns.pack(pady=6)
        self.btn = ctk.CTkButton(btns, text="▶  Старт", command=self.toggle, width=200, height=46,
                                 fg_color=GREEN, hover_color=GREEN_H, text_color="#0c1410",
                                 corner_radius=14, font=(FONT, 15, "bold"))
        self.btn.pack(side="left", padx=5)
        self.mute_btn = ctk.CTkButton(btns, text="🔊", command=self.toggle_mute, width=58, height=46,
                                      fg_color=FIELD, hover_color="#34344a", text_color=TEXT,
                                      corner_radius=14, font=(FONT, 16))
        self.mute_btn.pack(side="left", padx=5)
        self.test_btn = ctk.CTkButton(btns, text="Тест", command=self.test, width=80, height=46,
                                      fg_color=FIELD, hover_color="#34344a", text_color=TEXT,
                                      corner_radius=14, font=(FONT, 13, "bold"))
        self.test_btn.pack(side="left", padx=5)
        ctk.CTkLabel(self.root, text="F8 — пауза звука   •   F9 — старт/стоп   •   крестик сворачивает в трей",
                     text_color=GREY, font=(FONT, 10)).pack(pady=(2, 2))

        self.topmost_var = ctk.BooleanVar(value=bool(self.cfg.get("topmost", False)))
        ctk.CTkSwitch(self.root, text="Поверх всех окон", variable=self.topmost_var,
                      command=self._apply_topmost, progress_color=ACCENT,
                      text_color=SUB, font=(FONT, 11)).pack(pady=(0, 2))
        self._apply_topmost()

        inrow = ctk.CTkFrame(self.root, fg_color="transparent")
        inrow.pack(fill="x", padx=26, pady=(8, 2))
        inrow.columnconfigure(0, weight=1)
        self.text_entry = ctk.CTkEntry(inrow, placeholder_text="Введите текст и нажмите Enter — диктор озвучит…",
                                       fg_color=FIELD, text_color=TEXT, border_width=0,
                                       corner_radius=10, font=(FONT, 12), height=40)
        self.text_entry.grid(row=0, column=0, sticky="ew")
        self.text_entry.bind("<Return>", lambda e: self._say_typed())
        self.say_btn = ctk.CTkButton(inrow, text="Озвучить", width=92, height=40, command=self._say_typed,
                                     fg_color=ACCENT, hover_color=ACC_HOV, text_color="#0c1410",
                                     corner_radius=10, font=(FONT, 12, "bold"))
        self.say_btn.grid(row=0, column=1, padx=(8, 0))

        labrow = ctk.CTkFrame(self.root, fg_color="transparent")
        labrow.pack(fill="x", padx=30, pady=(8, 4))
        ctk.CTkLabel(labrow, text="РАСПОЗНАННАЯ РЕЧЬ", text_color=GREY,
                     font=(FONT, 10, "bold")).pack(side="left")
        ctk.CTkButton(labrow, text="Очистить", width=72, height=24, command=self._clear_log,
                      fg_color=FIELD, hover_color="#34344a", text_color=SUB,
                      corner_radius=8, font=(FONT, 10)).pack(side="right")
        self.log = ctk.CTkTextbox(self.root, fg_color=CARD, text_color=TEXT,
                                  font=("Consolas", 12), corner_radius=12, wrap="word")
        self.log.pack(fill="both", expand=True, padx=26, pady=(0, 22))

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

    def _input_devices(self):
        labels, result = self._query_devices("max_input_channels")
        if labels is None:
            self._log(f"Не удалось получить список микрофонов: {result}")
            return ["(нет устройств)"]
        self.input_device_map = result
        return labels or ["(нет устройств)"]

    def _cable_present(self, devices):
        """True, если среди устройств есть виртуальный микрофон VB-Cable."""
        return any("cable input" in d.lower() for d in devices)

    def _default_device(self, devices):
        for d in devices:
            if "cable input" in d.lower():
                return d
        return devices[0]

    def _default_input_device(self, devices):
        """Системный микрофон по умолчанию, если он есть в списке; иначе первый."""
        try:
            default_idx = sd.default.device[0]
            for label, idx in self.input_device_map.items():
                if idx == default_idx:
                    return label
        except Exception:
            pass
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
        return self._map_idx(self.device_map, self.device_var.get())

    def _input_device_idx(self):
        return self._map_idx(self.input_device_map, self.input_device_var.get())

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

    def refresh_input_devices(self):
        devices = self._input_devices()
        self.input_device_menu.configure(values=devices)
        if self.input_device_var.get() not in devices:
            self.input_device_var.set(self._default_input_device(devices))
        self._log("Список микрофонов обновлён.")

    def _on_setting_change(self, *args):
        self._save_settings()

    def _on_vol(self, val):
        self.vol_lbl.configure(text=f"{int(float(val))}%")
        self._save_settings()

    def _pitch_text(self, v):
        v = int(v)
        return f"{'+' if v > 0 else ''}{v}"

    def _on_pitch(self, val):
        self.pitch_lbl.configure(text=self._pitch_text(float(val)))
        self._save_settings()

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

    def _show(self):
        self.root.deiconify(); self.root.lift()

    def _hide_to_tray(self):
        if self.tray is not None:
            self.root.withdraw()
        else:
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
    def _start_mic_monitor(self):
        if self._mic_stream is not None:
            return
        def cb(indata, frames, time_info, status):
            try:
                self.mic_level = float(np.sqrt(np.mean(np.square(indata))))
            except Exception:
                self.mic_level = 0.0
        try:
            self._mic_stream = sd.InputStream(channels=1, device=self._input_device_idx(), callback=cb)
            self._mic_stream.start()
        except Exception as e:
            self._mic_stream = None
            self._log(f"Индикатор микрофона недоступен: {e}")

    def _stop_mic_monitor(self):
        self.mic_level = 0.0
        if self._mic_stream is not None:
            try:
                self._mic_stream.stop(); self._mic_stream.close()
            except Exception:
                pass
            self._mic_stream = None

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
            level = min(1.0, self.mic_level * 4.0)
            self.mic_bar.set(level)
            color = RED if level > 0.92 else YELLOW if level > 0.6 else GREEN
            self.mic_bar.configure(progress_color=color)
        except Exception:
            pass
        self.root.after(80, self._drain)

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
        if model_path in self._rvc_failed_paths:
            return samples, sr
        import tempfile
        with self._rvc_lock:
            in_path = out_path = None
            try:
                self._ensure_rvc(model_path)
                try:
                    self._rvc.set_params(f0up_key=int(self.pitch_var.get()))
                except Exception:
                    pass
                fd_in, in_path = tempfile.mkstemp(suffix=".wav"); os.close(fd_in)
                fd_out, out_path = tempfile.mkstemp(suffix=".wav"); os.close(fd_out)
                sf.write(in_path, samples, sr)
                self._rvc.infer_file(in_path, out_path)
                out, out_sr = sf.read(out_path, dtype="float32")
                return out, out_sr
            except Exception as e:
                self._rvc_failed_paths.add(model_path)
                self._log(f"RVC-конверсия недоступна ({e}). Играю базовым голосом; "
                          f"проверьте установку rvc-python и видеокарту.")
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
        try:
            mp3 = asyncio.run_coroutine_threadsafe(go(), self._loop).result(timeout=15)
        except Exception as e:
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
        v = max(0, min(100, int(self.vol_var.get()))) / 100.0
        with self._play_lock:
            try:
                sd.stop()
                sd.play(np.asarray(samples) * v, sr, device=device_idx)
                sd.wait()
            except Exception as e:
                self._log(f"Ошибка воспроизведения: {e}")

    def _resolve_translation(self, text, lang_disp, edge_voice):
        """Если для lang_disp задан перевод — переводит text и возвращает
        (перевод, голос перевода); иначе (text, edge_voice) без изменений.
        None, если перевод запрошен, но недоступен (сбой сети/таймаут)."""
        tcode, tvoice = LANGUAGES.get(lang_disp, (None, None))
        if not tcode:
            return text, edge_voice
        out = self._translate(text, tcode)
        if not out:
            return None
        self._log(f"→ {out}")
        return out, tvoice

    def _synth_convert_play(self, text, voice, rate, rvc_path):
        """Синтез -> (опционально) RVC-конверсия -> проигрывание. Общий конвейер
        для теста, озвучки набранного текста и основного цикла распознавания."""
        s, sr = self._synth(text, voice, rate)
        if s is not None and rvc_path:
            s, sr = self._convert_rvc(s, sr, rvc_path)
        if s is not None:
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
        # self.recorder читаем под тем же замком, что и _install_recorder/
        # _discard_recorder — иначе между проверкой "не None" и вызовом
        # .shutdown() рабочий поток мог успеть сбросить self.recorder в None
        # (например, по нажатию «Стоп»), и вызов ушёл бы в None.shutdown().
        with self._recorder_lock:
            rec = self.recorder
        if rec is None:
            return
        self._recorder_restart_pending = True
        self._log(reason_msg)
        self._status(YELLOW, "Перезапуск")
        try:
            rec.shutdown()
        except Exception:
            pass

    def _on_model_change(self, *args):
        self._save_settings()
        self._request_recorder_restart(f"Меняю модель распознавания на «{self.model_var.get()}»...")

    def _on_input_device_change(self, *args):
        self._save_settings()
        if self.running:
            self._stop_mic_monitor()
            self._start_mic_monitor()
        self._request_recorder_restart(f"Меняю микрофон на «{self.input_device_var.get()}»...")

    def toggle_mute(self):
        self.muted = not self.muted
        if self.muted:
            self.mute_btn.configure(text="🔇", fg_color="#3a2030")
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
            self._rvc_failed_paths.discard(rvc_path)
        rate = SPEEDS[self.speed_var.get()]
        self._log("Тест: воспроизвожу проверочную фразу...")
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
                self.root.after(0, lambda: self.test_btn.configure(state="normal"))
        threading.Thread(target=run, daemon=True).start()

    def _say_typed(self):
        text = self.text_entry.get().strip()
        if not text:
            return
        self.text_entry.delete(0, "end")
        self._log(f"⌨ {text}")
        edge_voice, rvc_path = self._resolve_voice(self.voice_var.get())
        lang_disp = self.lang_var.get()
        rate = SPEEDS[self.speed_var.get()]

        def run():
            try:
                resolved = self._resolve_translation(text, lang_disp, edge_voice)
                if resolved is None:
                    return
                out, out_voice = resolved
                self._synth_convert_play(out, out_voice, rate, rvc_path)
            except Exception as e:
                self._log(f"Ошибка озвучки текста: {e}")
        threading.Thread(target=run, daemon=True).start()

    def start(self):
        self.running = True
        self.btn.configure(text="■  Стоп", fg_color=RED, hover_color=RED_H)
        self._status(YELLOW, "Загрузка")
        self._save_settings()
        self._start_mic_monitor()
        threading.Thread(target=self._run, daemon=True).start()

    def _discard_recorder(self):
        # _recorder_lock делает чтение-и-сброс self.recorder атомарным относительно
        # рабочего потока (_run), который под тем же замком решает, можно ли
        # установить новый рекордер после перезапуска модели/распознавания —
        # без этого окно между "self.running ещё True" и "self.recorder = new"
        # позволяет новому рекордеру пережить stop()/_quit() и зависнуть без владельца.
        with self._recorder_lock:
            rec, self.recorder = self.recorder, None
        if rec is not None:
            try:
                rec.shutdown()
            except Exception:
                pass

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

    def stop(self):
        self.running = False
        self._stop_mic_monitor()
        self.btn.configure(text="▶  Старт", fg_color=GREEN, hover_color=GREEN_H)
        self._status(GREY, "Остановлено")
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
        self.root.after(0, self.stop)
        self.root.after(0, lambda: self._status(RED, "Ошибка"))

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
        self._log("Загрузка модели (при первом запуске — загрузка из интернета)...")

        def make_recorder():
            model = self.model_var.get()
            beam = MODEL_BEAM.get(model, 5)
            self._log(f"Модель: {model}  |  точность: float32  |  beam: {beam}")
            return AudioToTextRecorder(
                model=model, language="ru", spinner=False,
                device=device, compute_type=compute,
                post_speech_silence_duration=0.4,
                beam_size=beam,
                initial_prompt=WHISPER_PROMPT,
                input_device_index=self._input_device_idx(),
            )

        try:
            new_recorder = make_recorder()
        except Exception as e:
            self._log(f"Ошибка запуска: {e}")
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
        errors = 0
        while self.running:
            try:
                text = self.recorder.text()
                if not self.running:
                    break
                errors = 0
                text = (text or "").strip()
                if len(text) < 2:
                    continue
                if text == last_text:
                    continue
                last_text = text
                self._log(f"› {text}")
                if self.muted:
                    continue

                rate = SPEEDS[self.speed_var.get()]
                edge_voice, rvc_path = self._resolve_voice(self.voice_var.get())
                resolved = self._resolve_translation(text, self.lang_var.get(), edge_voice)
                if resolved is None:
                    continue
                out, out_voice = resolved

                self._status(GREEN, "Озвучивание")
                self._synth_convert_play(out, out_voice, rate, rvc_path)
                if self.running and not self.muted:
                    self._status(ACCENT, "Прослушивание")
            except Exception as e:
                if self._recorder_restart_pending and self.running:
                    self._recorder_restart_pending = False
                    try:
                        new_recorder = make_recorder()
                        if not self._install_recorder(new_recorder):
                            break
                        errors = 0
                        last_text = ""
                        self._log("Распознавание перезапущено с новыми настройками.")
                        if not self.muted:
                            self._status(ACCENT, "Прослушивание")
                    except Exception as e2:
                        self._log(f"Не удалось сменить модель: {e2}")
                        self._abort_run(recorder_dead=True)
                        break
                    continue
                self._log(f"Пропущено: {e}")
                errors += 1
                if errors >= 5 and self.running:
                    self._log("Слишком много ошибок подряд — перезапуск распознавания...")
                    self._status(YELLOW, "Перезапуск")
                    with self._recorder_lock:
                        old_recorder = self.recorder
                    try:
                        if old_recorder is not None:
                            old_recorder.shutdown()
                    except Exception:
                        pass
                    try:
                        new_recorder = make_recorder()
                        if not self._install_recorder(new_recorder):
                            break
                        errors = 0
                        self._log("Распознавание перезапущено.")
                        if not self.muted:
                            self._status(ACCENT, "Прослушивание")
                    except Exception as e2:
                        self._log(f"Не удалось перезапустить: {e2}")
                        self._abort_run(recorder_dead=True)
                        break
                continue


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
