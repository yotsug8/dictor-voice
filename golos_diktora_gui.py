import io
import os
import sys
import json
import asyncio
import threading
import queue

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
        self.root.geometry("560x935")
        self.root.minsize(520, 885)
        self.root.configure(fg_color=BG)

        self.running = False
        self.muted = False
        self.recorder = None
        self.device_map = {}
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
        self._rvc_failed = False
        self._rvc_lock = threading.Lock()
        self.rvc_voices = self._scan_rvc_voices()
        self.cfg = self._load_settings()

        self._build()
        self.root.after(80, self._drain)
        self._start_mic_monitor()
        self._setup_hotkey()
        self._setup_tray()
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)

    # ---------- settings ----------
    def _load_settings(self):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_settings(self):
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "voice": self.voice_var.get(),
                    "model": self.model_var.get(),
                    "device": self.device_var.get(),
                    "speed": self.speed_var.get(),
                    "lang": self.lang_var.get(),
                    "volume": int(self.vol_var.get()),
                    "topmost": bool(self.topmost_var.get()),
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

        card = ctk.CTkFrame(self.root, fg_color=CARD, corner_radius=16)
        card.pack(fill="x", padx=26, pady=14)
        card.columnconfigure(0, weight=1)
        card.columnconfigure(1, weight=1)

        def g(k, d, pool):
            v = self.cfg.get(k)
            return v if v in pool else d
        voice_names = list(VOICES) + list(self.rvc_voices)
        self.voice_var = ctk.StringVar(value=g("voice", list(VOICES)[0], voice_names))
        self.model_var = ctk.StringVar(value=g("model", "small", MODELS))
        self.speed_var = ctk.StringVar(value=g("speed", "Обычная", SPEEDS))
        self.lang_var = ctk.StringVar(value=g("lang", list(LANGUAGES)[0], LANGUAGES))
        self.vol_var = ctk.IntVar(value=int(self.cfg.get("volume", 100)))

        devices = self._devices()
        dev0 = self.cfg.get("device") if self.cfg.get("device") in devices else self._default_device(devices)
        self.device_var = ctk.StringVar(value=dev0)

        self._cap(card, "Голос диктора").grid(row=0, column=0, sticky="ew", padx=(18, 9), pady=(16, 2))
        self._menu(card, self.voice_var, voice_names).grid(row=1, column=0, sticky="ew", padx=(18, 9))
        self._cap(card, "Точность (модель)").grid(row=0, column=1, sticky="ew", padx=(9, 18), pady=(16, 2))
        self._menu(card, self.model_var, MODELS).grid(row=1, column=1, sticky="ew", padx=(9, 18))

        self._cap(card, "Скорость речи").grid(row=2, column=0, sticky="ew", padx=(18, 9), pady=(14, 2))
        self._menu(card, self.speed_var, list(SPEEDS)).grid(row=3, column=0, sticky="ew", padx=(18, 9))
        self._cap(card, "Перевод (диктор на языке)").grid(row=2, column=1, sticky="ew", padx=(9, 18), pady=(14, 2))
        self._menu(card, self.lang_var, list(LANGUAGES)).grid(row=3, column=1, sticky="ew", padx=(9, 18))

        volcap = ctk.CTkFrame(card, fg_color="transparent")
        volcap.grid(row=4, column=0, columnspan=2, sticky="ew", padx=18, pady=(14, 2))
        ctk.CTkLabel(volcap, text="Громкость диктора", text_color=SUB, font=(FONT, 12)).pack(side="left")
        self.vol_lbl = ctk.CTkLabel(volcap, text=f"{self.vol_var.get()}%", text_color=ACCENT, font=(FONT, 12))
        self.vol_lbl.pack(side="right")
        ctk.CTkSlider(card, from_=0, to=100, variable=self.vol_var, number_of_steps=100,
                      progress_color=ACCENT, button_color=ACCENT, button_hover_color=ACC_HOV,
                      fg_color=FIELD, command=self._on_vol).grid(row=5, column=0, columnspan=2, sticky="ew", padx=18)

        microw = ctk.CTkFrame(card, fg_color="transparent")
        microw.grid(row=6, column=0, columnspan=2, sticky="ew", padx=18, pady=(14, 2))
        microw.columnconfigure(1, weight=1)
        ctk.CTkLabel(microw, text="Микрофон", text_color=SUB, font=(FONT, 12)).grid(row=0, column=0, padx=(0, 10))
        self.mic_bar = ctk.CTkProgressBar(microw, progress_color=GREEN, fg_color=FIELD,
                                          height=12, corner_radius=6)
        self.mic_bar.grid(row=0, column=1, sticky="ew")
        self.mic_bar.set(0)

        self._cap(card, "Куда выводить звук").grid(row=7, column=0, columnspan=2, sticky="ew", padx=18, pady=(14, 2))
        devrow = ctk.CTkFrame(card, fg_color="transparent")
        devrow.grid(row=8, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 18))
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

    # ---------- devices ----------
    def _friendly(self, name):
        if "cable input" in name.lower():
            return "Виртуальный микрофон (CABLE Input)"
        if "(" in name and ")" not in name:
            name = name.split("(")[0].strip()
        return name

    def _devices(self):
        try:
            apis = [ha["name"] for ha in sd.query_hostapis()]
        except Exception:
            apis = []
        target = None
        for pref in ("Windows DirectSound", "Windows WASAPI", "MME"):
            if pref in apis:
                target = apis.index(pref); break
        self.device_map = {}
        labels = []
        for idx, dev in enumerate(sd.query_devices()):
            if dev["max_output_channels"] <= 0:
                continue
            if target is not None and dev["hostapi"] != target:
                continue
            low = dev["name"].lower()
            if "sound mapper" in low or "primary sound" in low or "первичный" in low:
                continue
            label = self._friendly(dev["name"])
            base, n = label, 2
            while label in self.device_map:
                label = f"{base} ({n})"; n += 1
            self.device_map[label] = idx
            labels.append(label)
        return labels or ["(нет устройств)"]

    def _default_device(self, devices):
        for d in devices:
            if "cable input" in d.lower():
                return d
        return devices[0]

    def _device_idx(self):
        return self.device_map.get(self.device_var.get(), 0)

    def refresh_devices(self):
        devices = self._devices()
        self.device_menu.configure(values=devices)
        if self.device_var.get() not in devices:
            self.device_var.set(self._default_device(devices))
        self._log("Список устройств обновлён.")

    def _on_vol(self, val):
        self.vol_lbl.configure(text=f"{int(float(val))}%")

    def _apply_topmost(self):
        try:
            self.root.attributes("-topmost", bool(self.topmost_var.get()))
        except Exception:
            pass

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
        except Exception:
            self.tray = None

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
        if self.recorder is not None:
            try:
                self.recorder.shutdown()
            except Exception:
                pass
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._mic_stream is not None:
            try:
                self._mic_stream.stop(); self._mic_stream.close()
            except Exception:
                pass
        self.root.destroy()

    # ---------- mic level ----------
    def _start_mic_monitor(self):
        def cb(indata, frames, time_info, status):
            try:
                self.mic_level = float(np.sqrt(np.mean(np.square(indata))))
            except Exception:
                self.mic_level = 0.0
        try:
            self._mic_stream = sd.InputStream(channels=1, callback=cb)
            self._mic_stream.start()
        except Exception as e:
            self._mic_stream = None
            self._log(f"Индикатор микрофона недоступен: {e}")

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
        import datetime
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
            self.mic_bar.set(min(1.0, self.mic_level * 4.0))
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
            return cands[0] if cands else ""
        except Exception:
            return ""

    def _ensure_rvc(self, model_path):
        """Лениво создаёт движок и (пере)загружает модель. Кэширует между фразами."""
        import torch
        from rvc_python.infer import RVCInference
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
        if self._rvc is None:
            self._log("RVC: инициализация движка (первый запуск может качать базовые модели)...")
            self._rvc = RVCInference(device=device)
        if self._rvc_loaded_path != model_path:
            index = self._find_index(model_path)
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
            self._rvc_loaded_path = model_path
            self._log(f"RVC: модель загружена ({os.path.basename(model_path)}).")

    def _convert_rvc(self, samples, sr, model_path):
        """Накладывает тембр RVC-модели на синтезированный звук. При сбое — базовый голос."""
        if self._rvc_failed:
            return samples, sr
        import tempfile
        with self._rvc_lock:
            in_path = out_path = None
            try:
                self._ensure_rvc(model_path)
                fd_in, in_path = tempfile.mkstemp(suffix=".wav"); os.close(fd_in)
                fd_out, out_path = tempfile.mkstemp(suffix=".wav"); os.close(fd_out)
                sf.write(in_path, samples, sr)
                self._rvc.infer_file(in_path, out_path)
                out, out_sr = sf.read(out_path, dtype="float32")
                return out, out_sr
            except Exception as e:
                self._rvc_failed = True
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
        import concurrent.futures
        def do():
            from deep_translator import GoogleTranslator
            return GoogleTranslator(source="ru", target=target).translate(text)
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            return ex.submit(do).result(timeout=10)
        except concurrent.futures.TimeoutError:
            self._log("Перевод: нет ответа от сервера (таймаут 10 с)")
            return None
        except Exception as e:
            self._log(f"Перевод недоступен: {e}")
            return None
        finally:
            ex.shutdown(wait=False)

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
        except Exception:
            return None, None
        if not mp3:
            return None, None
        return sf.read(io.BytesIO(mp3), dtype="float32")

    def _play(self, samples, sr, device_idx):
        v = max(0, min(100, int(self.vol_var.get()))) / 100.0
        with self._play_lock:
            try:
                sd.stop()
                sd.play(np.asarray(samples) * v, sr, device=device_idx)
                sd.wait()
            except Exception as e:
                self._log(f"Ошибка воспроизведения: {e}")

    # ---------- control ----------
    def toggle(self):
        self.stop() if self.running else self.start()

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
        rate = SPEEDS[self.speed_var.get()]
        idx = self._device_idx()
        self._log("Тест: воспроизвожу проверочную фразу...")

        def run():
            try:
                s, sr = self._synth(TEST_PHRASE, edge_voice, rate)
                if s is not None and rvc_path:
                    s, sr = self._convert_rvc(s, sr, rvc_path)
                if s is not None:
                    self._play(s, sr, idx)
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
        idx = self._device_idx()

        def run():
            try:
                tcode, tvoice = LANGUAGES.get(lang_disp, (None, None))
                if tcode:
                    out = self._translate(text, tcode)
                    if not out:
                        return
                    self._log(f"→ {out}")
                    out_voice = tvoice
                else:
                    out, out_voice = text, edge_voice
                s, sr = self._synth(out, out_voice, rate)
                if s is not None and rvc_path:
                    s, sr = self._convert_rvc(s, sr, rvc_path)
                if s is not None:
                    self._play(s, sr, idx)
            except Exception as e:
                self._log(f"Ошибка озвучки текста: {e}")
        threading.Thread(target=run, daemon=True).start()

    def start(self):
        self.running = True
        self.btn.configure(text="■  Стоп", fg_color=RED, hover_color=RED_H)
        self._status(YELLOW, "Загрузка")
        self._save_settings()
        idx = self._device_idx()
        model = self.model_var.get()
        threading.Thread(target=self._run, args=(idx, model), daemon=True).start()

    def stop(self):
        self.running = False
        self.btn.configure(text="▶  Старт", fg_color=GREEN, hover_color=GREEN_H)
        self._status(GREY, "Остановлено")
        self._log("Остановка.")
        if self.recorder is not None:
            try:
                self.recorder.shutdown()
            except Exception:
                pass
            self.recorder = None

    # ---------- worker ----------
    def _run(self, device_idx, model):
        try:
            import torch
            from RealtimeSTT import AudioToTextRecorder
        except Exception as e:
            self._log(f"Не установлены библиотеки: {e}")
            self._status(RED, "Ошибка"); self.running = False
            return

        device = "cuda" if torch.cuda.is_available() else "cpu"
        # float32 на обоих устройствах -> одинаковая точность распознавания
        compute = "float32"
        beam = MODEL_BEAM.get(model, 5)
        self._log(f"Устройство: {'видеокарта (cuda)' if device=='cuda' else 'процессор (cpu)'}  |  точность: float32  |  beam: {beam}")
        self._log("Загрузка модели (при первом запуске — загрузка из интернета)...")

        def make_recorder():
            return AudioToTextRecorder(
                model=model, language="ru", spinner=False,
                device=device, compute_type=compute,
                post_speech_silence_duration=0.4,
                beam_size=beam,
                initial_prompt=WHISPER_PROMPT,
            )

        try:
            self.recorder = make_recorder()
        except Exception as e:
            self._log(f"Ошибка запуска: {e}")
            self._status(RED, "Ошибка"); self.running = False
            return

        self._log("Готово к работе.")
        self._status(ACCENT, "Прослушивание")
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

                tcode, tvoice = LANGUAGES.get(self.lang_var.get(), (None, None))
                rate = SPEEDS[self.speed_var.get()]
                edge_voice, rvc_path = self._resolve_voice(self.voice_var.get())
                if tcode:
                    out = self._translate(text, tcode)
                    if not out:
                        continue
                    self._log(f"→ {out}")
                    out_voice = tvoice
                else:
                    out, out_voice = text, edge_voice

                self._status(GREEN, "Озвучивание")
                s, sr = self._synth(out, out_voice, rate)
                if s is not None and rvc_path:
                    s, sr = self._convert_rvc(s, sr, rvc_path)
                if s is not None:
                    self._play(s, sr, device_idx)
                if self.running and not self.muted:
                    self._status(ACCENT, "Прослушивание")
            except Exception as e:
                self._log(f"Пропущено: {e}")
                errors += 1
                if errors >= 5 and self.running:
                    self._log("Слишком много ошибок подряд — перезапуск распознавания...")
                    self._status(YELLOW, "Перезапуск")
                    try:
                        self.recorder.shutdown()
                    except Exception:
                        pass
                    try:
                        self.recorder = make_recorder()
                        errors = 0
                        self._log("Распознавание перезапущено.")
                        self._status(ACCENT, "Прослушивание")
                    except Exception as e2:
                        self._log(f"Не удалось перезапустить: {e2}")
                        self._status(RED, "Ошибка"); self.running = False
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
