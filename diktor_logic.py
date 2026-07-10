"""Чистая логика приложения «Голос Диктора», без GUI и без внешних зависимостей.

Вынесено в отдельный модуль, чтобы:
  1) убрать дублирование и держать «правила» в одном месте;
  2) покрыть тестами то, что раньше жило внутри GUI-класса и не тестировалось
     (кулдаун RVC, подавление дубликатов, обрезка настроек, уровень индикатора,
     подбор устройства вывода) — именно здесь регулярно всплывали баги.

Модуль намеренно не импортирует customtkinter/torch/sounddevice, поэтому
запускается и тестируется в любой среде, в том числе без дисплея и звука.
"""

CABLE_MARK = "cable input"


def clamp(value, lo, hi):
    """Зажимает value в [lo, hi]."""
    return max(lo, min(hi, value))


def clamp_int(value, lo, hi, default):
    """Как clamp, но приводит к int и возвращает default при мусоре."""
    try:
        return clamp(int(value), lo, hi)
    except (TypeError, ValueError):
        return default


def format_pitch(v):
    """Тон RVC -> подпись со знаком: 0, +3, -5."""
    v = int(v)
    return f"{'+' if v > 0 else ''}{v}"


def friendly_device_name(name):
    """Человекочитаемое имя устройства вывода."""
    if CABLE_MARK in name.lower():
        return "Виртуальный микрофон (CABLE Input)"
    if "(" in name and ")" not in name:
        name = name.split("(")[0].strip()
    return name


def has_cable(devices):
    """True, если среди устройств есть виртуальный микрофон VB-Cable."""
    return any(CABLE_MARK in d.lower() for d in devices)


def default_output(devices):
    """Устройство вывода по умолчанию: VB-Cable, если есть; иначе первое."""
    if not devices:
        return ""
    for d in devices:
        if CABLE_MARK in d.lower():
            return d
    return devices[0]


def map_device_index(device_map, selected_label):
    """Индекс выбранного устройства вывода. Если метка устарела (устройство
    пропало) — берём первое доступное, а не произвольный индекс 0."""
    if not device_map:
        return None
    if selected_label in device_map:
        return device_map[selected_label]
    return next(iter(device_map.values()))


def meter_level(rms, mult=2.5):
    """RMS -> заполнение полоски индикатора [0..1]. NaN/мусор -> 0.
    Множитель 2.5: RMS речи в норме 0.05–0.3, при *4 полоска упиралась в максимум."""
    if rms != rms:  # NaN
        return 0.0
    return clamp(rms * mult, 0.0, 1.0)


def rvc_in_cooldown(failed_at, now, cooldown):
    """True, если RVC-модель недавно сбоила и ещё в кулдауне (пропускаем конверсию)."""
    return failed_at is not None and (now - failed_at) < cooldown


def is_duplicate(text, last_text, now, last_time, window=3.0):
    """True, если ту же фразу нужно подавить как дубликат: она совпадает с
    прошлой И пришла почти сразу (в пределах окна). Осознанный повтор спустя
    паузу дубликатом не считается."""
    return text == last_text and (now - last_time) < window


def is_speakable(text, min_len=2):
    """Стоит ли озвучивать распознанный текст (не пусто и не короче min_len)."""
    return len(text.strip()) >= min_len
