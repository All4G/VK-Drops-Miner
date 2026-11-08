"""Диагностика: показывает видимые окна (класс, PID, заголовок).
Окно Chromium от Playwright — класс Chrome_WidgetWin_1. Запускайте вместе с main.py,
чтобы убедиться, что оно присутствует в списке."""
import ctypes
import ctypes.wintypes as wt

user32 = ctypes.windll.user32
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)


def enum_handler(hwnd, _lparam):
    if user32.IsWindowVisible(hwnd):
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, length + 1)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            print(f"HWND={hwnd} | PID={pid.value} | class={cls.value} | title='{title.value}'")
    return True


print("=== ВСЕ ВИДИМЫЕ ОКНА ===")
user32.EnumWindows(WNDENUMPROC(enum_handler), 0)
