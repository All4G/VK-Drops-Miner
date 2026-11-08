@echo off
rem Локальная сборка VKDropsMiner.exe (Windows). Результат: dist\VKDropsMiner\VKDropsMiner.exe
setlocal
python -m pip install -r requirements-dev.txt || goto :err
rmdir /s /q build dist 2>nul

python -m PyInstaller --noconfirm --clean --windowed ^
  --name VKDropsMiner ^
  --icon assets\icon.ico ^
  --collect-all customtkinter ^
  --collect-all playwright ^
  --hidden-import pystray._win32 ^
  main.py || goto :err

echo.
echo Готово: dist\VKDropsMiner\VKDropsMiner.exe
echo Браузер Chromium скачается при первом запуске (~150 МБ).
exit /b 0
:err
echo Сборка не удалась.
exit /b 1
