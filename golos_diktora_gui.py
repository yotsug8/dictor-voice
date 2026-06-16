import io
import asyncio
import threading
import queue

import tkinter as tk
from tkinter import scrolledtext

import sounddevice as sd
import soundfile as sf
import edge_tts


# ---- palette (Catppuccin Mocha) ----
BG      = "#1e1e2e"
SURFACE = "#181825"
CARD    = "#313244"
HOVER   = "#45475a"
TEXT    = "#cdd6f4"
SUBTEXT = "#a6adc8"
BLUE    = "#89b4fa"
GREEN   = "#a6e3a1"
RED     = "#f38ba8"
YELLOW  = "#f9e2af"
GREY    = "#585b70"

VOICES = {
    "Дмитрий (мужской)": "ru-RU-DmitryNeural",
    "Светлана (женский)": "ru-RU-SvetlanaNeural",
}
MODELS = ["tiny", "base", "small", "medium"]


class RoundButton(tk.Canvas):
    def __init__(self, parent, text, command, fill, hover, fg="#11111b",
                 width=240, height=54, radius=16):
        super().__init__(parent, width=width, height=height, bg=parent["bg"],
                         highlightthickness=0)
        self.command = command
        self.fill, self.hover, self.fg = fill, hover, fg
        self.w, self.h, self.r = width, height, radius
        self._text = text
        self._draw(fill)
        self.bind("<Enter>", lambda e: self._draw(self.hover))
        self.bind("<Leave>", lambda e: self._draw(self.fill))
        self.bind("<Button-1>", lambda e: self.command())

    def _round(self, x1, y1, x2, y2, r, **kw):
        pts = [x1+r, y1, x2-r, y1, x2, y1, x2, y1+r, x2, y2-r, x2, y2,
               x2-r, y2, x1+r, y2, x1, y2, x1, y2-r, x1, y1+r, x1, y1]
        return self.create_polygon(pts, smooth=True, **kw)

    def _draw(self, color):
        self.delete("all")
        self._round(2, 2, self.w-2, self.h-2, self.r, fill=color)
        self.create_text(self.w/2, self.h/2, text=self._text,
                         fill=self.fg, font=("Segoe UI", 13, "bold"))

    def set(self, text, fill, hover):
        self._text, self.fill, self.hover = text, fill, hover
        self._draw(fill)


class DiktorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Голос Диктора")
        self.root.geometry("600x620")
        self.root.minsize(560, 580)
        self.root.configure(bg=BG)

        self.running = False
        self.recorder = None
        self.ui_queue = queue.Queue()

        self._build()
        self.root.after(80, self._drain)

    # ---------- helpers ----------
    def _dropdown(self, parent, variable, options):
        om = tk.OptionMenu(parent, variable, *options)
        om.config(bg=CARD, fg=TEXT, activebackground=HOVER, activeforeground=TEXT,
                  relief="flat", bd=0, highlightthickness=0, anchor="w",
                  font=("Segoe UI", 10), cursor="hand2", padx=12, pady=7,
                  indicatoron=True)
        om["menu"].config(bg=CARD, fg=TEXT, activebackground=BLUE,
                          activeforeground="#11111b", relief="flat", bd=0,
                          font=("Segoe UI", 10), tearoff=0)
        return om

    def _cap(self, parent, text):
        return tk.Label(parent, text=text, bg=parent["bg"], fg=SUBTEXT,
                        font=("Segoe UI", 10))

    def _build(self):
        # header
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=28, pady=(22, 6))
        tk.Label(head, text="Голос Диктора", bg=BG, fg=TEXT,
                 font=("Segoe UI", 22, "bold")).pack(side="left")
        st = tk.Frame(head, bg=BG)
        st.pack(side="right", pady=6)
        self.dot = tk.Canvas(st, width=14, height=14, bg=BG, highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 7))
        self._dot_id = self.dot.create_oval(2, 2, 12, 12, fill=GREY, outline="")
        self.status_lbl = tk.Label(st, text="Остановлено", bg=BG, fg=SUBTEXT,
                                   font=("Segoe UI", 10))
        self.status_lbl.pack(side="left")

        tk.Label(self.root, text="Говори — после паузы диктор повторит твоими словами",
                 bg=BG, fg=SUBTEXT, font=("Segoe UI", 9)).pack(anchor="w", padx=28)

        # settings card
        card = tk.Frame(self.root, bg=CARD)
        card.pack(fill="x", padx=28, pady=18)
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=18, pady=16)

        self.voice_var = tk.StringVar(value=list(VOICES.keys())[0])
        self.model_var = tk.StringVar(value="small")
        devices = self._devices()
        self.device_var = tk.StringVar(value=self._default_device(devices))

        self._cap(inner, "Голос диктора").grid(row=0, column=0, sticky="w", pady=(0, 2))
        self._dropdown(inner, self.voice_var, list(VOICES.keys())).grid(
            row=1, column=0, sticky="ew", pady=(0, 12), padx=(0, 8))

        self._cap(inner, "Точность (модель)").grid(row=0, column=1, sticky="w", pady=(0, 2))
        self._dropdown(inner, self.model_var, MODELS).grid(
            row=1, column=1, sticky="ew", pady=(0, 12))

        self._cap(inner, "Куда выводить звук").grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 2))
        self._dropdown(inner, self.device_var, devices).grid(
            row=3, column=0, columnspan=2, sticky="ew")

        inner.columnconfigure(0, weight=1)
        inner.columnconfigure(1, weight=1)

        # button
        self.btn = RoundButton(self.root, "▶   Старт", self.toggle, GREEN, "#b9f0b4")
        self.btn.pack(pady=8)

        # transcript
        tk.Label(self.root, text="РАСПОЗНАННАЯ РЕЧЬ", bg=BG, fg=GREY,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=30, pady=(8, 4))
        wrap = tk.Frame(self.root, bg=SURFACE)
        wrap.pack(fill="both", expand=True, padx=28, pady=(0, 22))
        self.log = scrolledtext.ScrolledText(wrap, bg=SURFACE, fg=TEXT,
                                             font=("Consolas", 10), relief="flat",
                                             wrap="word", borderwidth=0,
                                             insertbackground=TEXT)
        self.log.pack(fill="both", expand=True, padx=14, pady=12)
        self.log.tag_config("you", foreground=BLUE)
        self.log.tag_config("sys", foreground=SUBTEXT)
        self.log.tag_config("err", foreground=RED)

    def _devices(self):
        # only MME host API to avoid the same device repeating across host APIs
        mme = None
        try:
            for i, ha in enumerate(sd.query_hostapis()):
                if ha["name"] == "MME":
                    mme = i
                    break
        except Exception:
            mme = None
        out = []
        for idx, dev in enumerate(sd.query_devices()):
            if dev["max_output_channels"] > 0 and (mme is None or dev["hostapi"] == mme):
                out.append(f"[{idx}] {dev['name']}")
        return out or ["[0] (нет устройств)"]

    def _default_device(self, devices):
        for d in devices:
            if "cable input" in d.lower():
                return d
        return devices[0]

    # ---------- ui queue ----------
    def _log(self, msg, tag="sys"):
        self.ui_queue.put(("log", (msg, tag)))

    def _status(self, color, label):
        self.ui_queue.put(("status", (color, label)))

    def _drain(self):
        while not self.ui_queue.empty():
            kind, payload = self.ui_queue.get()
            if kind == "log":
                msg, tag = payload
                self.log.insert("end", msg + "\n", tag)
                self.log.see("end")
            else:
                color, label = payload
                self.dot.itemconfig(self._dot_id, fill=color)
                self.status_lbl.config(text=label, fg=color)
        self.root.after(80, self._drain)

    # ---------- control ----------
    def toggle(self):
        self.stop() if self.running else self.start()

    def start(self):
        self.running = True
        self.btn.set("■   Стоп", RED, "#f7a8bf")
        self._status(YELLOW, "Загрузка")
        idx = int(self.device_var.get().split("]")[0].strip("[ "))
        voice = VOICES[self.voice_var.get()]
        model = self.model_var.get()
        threading.Thread(target=self._run, args=(idx, voice, model), daemon=True).start()

    def stop(self):
        self.running = False
        self.btn.set("▶   Старт", GREEN, "#b9f0b4")
        self._status(GREY, "Остановлено")
        self._log("Остановка.")
        if self.recorder is not None:
            try:
                self.recorder.shutdown()
            except Exception:
                pass
            self.recorder = None

    # ---------- worker ----------
    def _synth(self, text, voice):
        async def go():
            comm = edge_tts.Communicate(text, voice)
            data = b""
            async for ch in comm.stream():
                if ch["type"] == "audio":
                    data += ch["data"]
            return data
        mp3 = asyncio.run(go())
        if not mp3:
            return None, None
        return sf.read(io.BytesIO(mp3), dtype="float32")

    def _run(self, device_idx, voice, model):
        try:
            import torch
            from RealtimeSTT import AudioToTextRecorder
        except Exception as e:
            self._log(f"Не установлены библиотеки: {e}", "err")
            self._status(RED, "Ошибка")
            self.running = False
            return

        device = "cuda" if torch.cuda.is_available() else "cpu"
        compute = "float16" if device == "cuda" else "int8"
        self._log(f"Устройство: {'видеокарта (cuda)' if device=='cuda' else 'процессор (cpu)'}")
        self._log("Загружаю модель (в первый раз скачается)...")

        try:
            self.recorder = AudioToTextRecorder(
                model=model, language="ru", spinner=False,
                device=device, compute_type=compute,
                post_speech_silence_duration=0.4,
            )
        except Exception as e:
            self._log(f"Ошибка запуска: {e}", "err")
            self._status(RED, "Ошибка")
            self.running = False
            return

        self._log("Готово. Говори!")
        self._status(BLUE, "Слушаю")
        while self.running:
            try:
                text = self.recorder.text()
                if not self.running:
                    break
                text = (text or "").strip()
                if not text:
                    continue
                self._log(f"› {text}", "you")
                self._status(GREEN, "Озвучиваю")
                samples, sr = self._synth(text, voice)
                if samples is not None:
                    sd.play(samples, sr, device=device_idx)
                    sd.wait()
                if self.running:
                    self._status(BLUE, "Слушаю")
            except Exception as e:
                self._log(f"Пропуск: {e}", "err")
                continue


def _resource(name):
    # works both when run as .py and when bundled by PyInstaller
    import os, sys
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    root = tk.Tk()
    
    try:
        root.iconbitmap(_resource("icon.ico"))
    except Exception:
        pass
    DiktorApp(root)
    root.mainloop()
