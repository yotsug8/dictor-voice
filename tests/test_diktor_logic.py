"""Тесты чистой логики (diktor_logic). Запуск: python -m pytest -q"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diktor_logic as dl


def test_clamp():
    assert dl.clamp(5, 0, 10) == 5
    assert dl.clamp(-3, 0, 10) == 0
    assert dl.clamp(99, 0, 10) == 10


def test_clamp_int_garbage():
    assert dl.clamp_int("abc", 0, 100, 100) == 100
    assert dl.clamp_int(None, -12, 12, 0) == 0
    assert dl.clamp_int("50", 0, 100, 100) == 50
    assert dl.clamp_int(250, 0, 100, 100) == 100


def test_format_pitch():
    assert dl.format_pitch(0) == "0"
    assert dl.format_pitch(3) == "+3"
    assert dl.format_pitch(-5) == "-5"
    assert dl.format_pitch(12.0) == "+12"


def test_friendly_device_name():
    assert dl.friendly_device_name("CABLE Input (VB-Audio Virtual Cable)") == \
        "Виртуальный микрофон (CABLE Input)"
    # незакрытая скобка -> отрезаем хвост
    assert dl.friendly_device_name("Динамики (Realtek High Definition") == "Динамики"
    # нормальное имя не трогаем
    assert dl.friendly_device_name("Наушники (2- USB)") == "Наушники (2- USB)"


def test_has_cable():
    assert dl.has_cable(["Динамики", "Виртуальный микрофон (CABLE Input)"]) is True
    assert dl.has_cable(["Динамики", "Наушники"]) is False
    assert dl.has_cable([]) is False


def test_default_output():
    assert dl.default_output([]) == ""
    assert dl.default_output(["Динамики", "Наушники"]) == "Динамики"
    assert dl.default_output(["Динамики", "Виртуальный микрофон (CABLE Input)"]) == \
        "Виртуальный микрофон (CABLE Input)"


def test_map_device_index():
    m = {"Динамики": 3, "Наушники": 5}
    assert dl.map_device_index(m, "Наушники") == 5
    # устаревшая метка -> первое доступное, не 0
    assert dl.map_device_index(m, "Пропавшее") == 3
    assert dl.map_device_index({}, "что угодно") is None
    assert dl.map_device_index(None, "что угодно") is None


def test_meter_level():
    assert dl.meter_level(0.0) == 0.0
    assert dl.meter_level(0.2) == 0.5
    assert dl.meter_level(1.0) == 1.0  # зажат сверху
    assert dl.meter_level(float("nan")) == 0.0  # NaN не пускаем в максимум
    assert dl.meter_level(-0.1) == 0.0  # зажат снизу


def test_rvc_in_cooldown():
    assert dl.rvc_in_cooldown(None, 100.0, 60) is False
    assert dl.rvc_in_cooldown(50.0, 80.0, 60) is True   # прошло 30 < 60
    assert dl.rvc_in_cooldown(50.0, 120.0, 60) is False  # прошло 70 >= 60


def test_is_duplicate():
    assert dl.is_duplicate("да", "да", 10.0, 9.0, 3.0) is True   # повтор через 1с
    assert dl.is_duplicate("да", "да", 20.0, 10.0, 3.0) is False  # повтор через 10с — ок
    assert dl.is_duplicate("да", "нет", 10.0, 9.5, 3.0) is False  # разные фразы


def test_is_speakable():
    assert dl.is_speakable("привет") is True
    assert dl.is_speakable(" а ") is False  # 1 символ после strip
    assert dl.is_speakable("   ") is False
    assert dl.is_speakable("ок") is True
