# PyInstaller-хук для webrtcvad (зависимость RealtimeSTT).
# Без него сборка падает на анализе webrtcvad. Пустой datas подавляет
# автоопределение ресурсов, которое для этого пакета не срабатывает.
# Подключается флагом --additional-hooks-dir=. в sborka_exe.bat.
datas = []
