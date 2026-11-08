"""
VK Live Drops Miner (CustomTkinter + Playwright)

Architecture:
- Main thread:        CustomTkinter GUI (the only thread that touches widgets)
- Background thread:  one asyncio event loop that owns Playwright
- VKMinerWorker:      all browser logic; talks to the GUI ONLY through a queue
- TrayIcon:           optional system-tray icon (pystray) to hide/show the browser

Browser "hide to background":
  The Playwright Chromium window is found by a unique command-line tag (so a
  regular Chrome of the user is never touched) and hidden with SW_HIDE. It then
  has no taskbar button and is invisible, but keeps running. A small init-script
  makes the page believe it is still visible, so the stream keeps playing.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wt
import copy
import logging
import logging.handlers
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import customtkinter as ctk
import yaml
from playwright.async_api import async_playwright

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:  # tray is optional
    pystray = None

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
__version__ = "1.0.0"
APP_NAME = "VK Live Drops Miner"
FROZEN = bool(getattr(sys, "frozen", False))  # running inside a PyInstaller exe


def _resolve_data_dir() -> Path:
    """Where config / session / logs live.

    Script: next to main.py. Exe: next to the exe (portable); if that folder is not
    writable (e.g. Program Files) -> %APPDATA%/VKDropsMiner.
    """
    if not FROZEN:
        return Path(__file__).resolve().parent
    exe_dir = Path(sys.executable).resolve().parent
    try:
        probe = exe_dir / ".write_test"
        probe.write_text("x")
        probe.unlink()
        return exe_dir
    except OSError:
        d = Path(os.environ.get("APPDATA", str(Path.home()))) / "VKDropsMiner"
        d.mkdir(parents=True, exist_ok=True)
        return d


BASE_DIR = DATA_DIR = _resolve_data_dir()
if FROZEN:  # Chromium is downloaded on first run into a folder next to the exe
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(DATA_DIR / "browsers"))
AUTH_FILE = BASE_DIR / "vk_auth.json"
CONFIG_FILE = BASE_DIR / "config.yaml"
DEBUG_DIR = BASE_DIR / "debug"
DEBUG_DUMP = os.environ.get("VK_MINER_DEBUG") == "1"  # dump pages on every refresh

CAMPAIGNS_URL = "https://live.vkvideo.ru/app/box/campaigns"

# Campaign sections that can be mined. "Активные" = already has progress,
# "Доступные" = not started yet. Upcoming / finished ones can't be mined.
MINEABLE_SECTIONS = ("Активные", "Доступные")

# --- Selectors (CSS-module class names have a hash suffix -> match by prefix) ---
CAMPAIGN_CARD_SELECTOR = '[class*="DropCampaignsListItem_root_"]'
SECTION_TITLE_SELECTOR = '[class*="DropCampaigns_listTitleWrapper_"]'
WATCH_BUTTON_SELECTOR = 'button:has-text("Смотреть стрим")'
LOGIN_BUTTON_SELECTOR = ':is(button, a):has-text("Войти")'
LOGGED_IN_SELECTOR = '[class*="TopMenuRightAuthorized"]'
NO_STREAMS_TEXT = "Не нашли подходящих трансляций"
LINK_ACCOUNT_BUTTONS = ("Подключить VK ID", "Подключить VK Play")

# Timings
IDLE_TIMEOUT_S = 90        # no progress widget at all on a stream -> try another stream
MAX_ROUNDS = 5000          # safety cap for stream re-openings per campaign
MAX_NO_STREAM_ATTEMPTS = 20  # "Не нашли подходящих трансляций" in a row (30 s apart)
AUTH_SAVE_EVERY_S = 900

STATUS_TEXT = {
    "done": "✅ завершено",
    "no_progress": "ℹ нет прогресса",
    "failed": "❌ ошибка",
    "stopped": "⏹ остановлено",
}

DEFAULT_CONFIG: dict = {
    "browser": {"mute_audio": True, "start_hidden": False, "headless": False},
    "drops": {
        "check_interval_seconds": 30,
        "claim_delay_min": 2,
        "claim_delay_max": 5,
        "stall_timeout_minutes": 10,
        "stream_start_timeout_seconds": 15,  # max wait for the player after pressing «Смотреть стрим»
        "offline_check_seconds": 4,          # streamer not live for this long -> retry at once (0 = off)
        "max_start_attempts": 100,           # failed «Смотреть стрим» attempts in a row before skipping
        "preferred_games": [],
        "blacklist_games": [],
    },
    "gui": {"show_notifications": True, "start_in_tray": False},
    "logging": {"level": "INFO", "file": "logs/miner.log", "max_size_mb": 10, "backup_count": 5},
}

# Written next to the program on first run (also shipped as config.example.yaml)
DEFAULT_CONFIG_YAML = """\
# VK Live Drops Miner — настройки
# Все параметры необязательны: если ключа нет, берётся значение по умолчанию.
# После изменения перезапустите программу.

browser:
  mute_audio: true        # звук браузера выключен
  start_hidden: false     # true — браузер сразу запускается скрытым (окно создаётся за экраном, без мигания)
  headless: false         # true — браузера с окном нет ВООБЩЕ. Войти в VK нужно один раз с headless: false,
                          # сессия сохранится в vk_auth.json

drops:
  check_interval_seconds: 30        # как часто читать прогресс (5..120)
  claim_delay_min: 2                # пауза перед нажатием «Получить» (сек)
  claim_delay_max: 5
  stall_timeout_minutes: 10         # прогресс не растёт столько минут -> открыть другой стрим
  stream_start_timeout_seconds: 15  # сколько ждать плеер после нажатия «Смотреть стрим»
  offline_check_seconds: 4          # стример не в эфире дольше N сек -> сразу нажать «Смотреть стрим» снова (0 = выкл.)
  max_start_attempts: 100           # столько неудачных запусков стрима подряд, прежде чем пропустить кампанию
  preferred_games: []               # например ["Мир кораблей", "War Thunder"] — такие кампании выше в списке
  blacklist_games: []               # кампании этих игр не показываются

gui:
  show_notifications: true   # всплывающее уведомление из трея при получении награды
  start_in_tray: false       # true — окно программы сразу уходит в трей

logging:
  level: "INFO"
  file: "logs/miner.log"
  max_size_mb: 10
  backup_count: 5
"""

# Makes the page think the tab is always visible/focused, so the player keeps
# running and counting watch time while the Chromium window is hidden.
VISIBILITY_SPOOF_JS = r"""
(() => {
  try {
    const def = (proto, prop, val) =>
      Object.defineProperty(proto, prop, { get: () => val, configurable: true });
    def(Document.prototype, 'hidden', false);
    def(Document.prototype, 'visibilityState', 'visible');
    def(Document.prototype, 'webkitHidden', false);
    def(Document.prototype, 'webkitVisibilityState', 'visible');
    Document.prototype.hasFocus = () => true;
    const stop = (e) => e.stopImmediatePropagation();
    document.addEventListener('visibilitychange', stop, true);
    document.addEventListener('webkitvisibilitychange', stop, true);
  } catch (e) {}
})();
"""

# <vk-video-player> is a web component with a Shadow DOM: document.querySelector('video')
# can NOT see the <video>. The light-DOM .state-container carries data-playback-state.
PLAYER_STATE_JS = r"""
() => {
  const players = Array.from(document.querySelectorAll('vk-video-player'));
  let state = null;
  for (const p of players) {
    const el = p.querySelector('[data-playback-state]');
    if (el) { state = el.getAttribute('data-playback-state'); break; }
  }
  const videos = [];
  const walk = (root) => {
    if (!root) return;
    root.querySelectorAll('video').forEach(v => videos.push(v));
    root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) walk(el.shadowRoot); });
  };
  players.forEach(p => { walk(p); if (p.shadowRoot) walk(p.shadowRoot); });
  const playingVideo = videos.find(v => !v.paused && !v.ended && v.readyState >= 2);
  return {
    present: players.length > 0,
    state: state,
    videos: videos.length,
    playing: state === 'playing' || !!playingVideo,
    online: !!document.querySelector('[class*="StreamStatus_isOnline"], [class*="ViewersCounter_isLive"]'),
    panel: !!document.querySelector('[class*="ChannelStreamPanel_root"]'),
    url: location.href,
  };
}
"""

TRY_PLAY_JS = r"""
() => {
  const walk = (root, out) => {
    if (!root) return;
    root.querySelectorAll('video').forEach(v => out.push(v));
    root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, out); });
  };
  const videos = [];
  document.querySelectorAll('vk-video-player').forEach(p => { walk(p, videos); walk(p.shadowRoot, videos); });
  let n = 0;
  videos.forEach(v => { if (v.paused) { n++; v.play().catch(() => {}); } });
  return n;
}
"""

# Reads the drop widget at the bottom of the chat.
#   kind = 'ring'       -> progress ring with a percent (< 100)
#   kind = 'claimable'  -> 100% or the ring turned into a box icon (reward can be claimed)
#   kind = 'none'       -> no widget (no campaign on this channel / all rewards claimed)
#   kind = 'nofooter'   -> chat footer not rendered (chat collapsed / page not ready)
DROP_WIDGET_JS = r"""
() => {
  const chatFooter = document.querySelector('[class*="ChatFooter_root"]');
  if (!chatFooter) return { kind: 'nofooter' };
  const ring = chatFooter.querySelector('[class*="CircularProgressBar_root"]');
  if (ring) {
    const pe = ring.querySelector('[class*="countdownPercent"]');
    const txt = ((pe ? pe.textContent : ring.textContent) || '').trim();
    const m = txt.match(/(\d+(?:[.,]\d+)?)\s*%/);
    const percent = m ? parseFloat(m[1].replace(',', '.')) : null;
    return { kind: (percent !== null && percent >= 100) ? 'claimable' : 'ring', percent: percent, text: txt };
  }
  const sf = chatFooter.querySelector('[class*="StreamChatFooter_root"]');
  if (sf) {
    for (const el of sf.children) {
      if (/PointActions/.test(el.className || '')) continue;
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) {
        return { kind: 'claimable', percent: 100, text: 'box', html: el.outerHTML.slice(0, 400) };
      }
    }
  }
  return { kind: 'none' };
}
"""

# Viewport coordinates of the drop widget (ring or box icon): we click its innermost
# visible element, like a human would, instead of a (possibly wider) wrapper.
DROP_WIDGET_POINT_JS = r"""
() => {
  const chatFooter = document.querySelector('[class*="ChatFooter_root"]');
  if (!chatFooter) return null;
  const vis = (n) => { const r = n.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  let el = chatFooter.querySelector('[class*="CircularProgressBar_root"]');
  if (!el) {
    const sf = chatFooter.querySelector('[class*="StreamChatFooter_root"]');
    if (sf) for (const c of sf.children) {
      if (/PointActions/.test(c.className || '')) continue;
      if (vis(c)) { el = c; break; }
    }
    if (el) { for (;;) { const c = Array.from(el.children).find(vis); if (!c) break; el = c; } }
  }
  if (!el) return null;
  el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
}
"""

logger = logging.getLogger("vk_miner")


# --------------------------------------------------------------------------- #
# Config / logging
# --------------------------------------------------------------------------- #
def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config() -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    try:
        if not CONFIG_FILE.exists():
            CONFIG_FILE.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                _deep_merge(cfg, yaml.safe_load(f) or {})
    except Exception as e:
        print(f"config.yaml не прочитан, использую значения по умолчанию: {e}")
    return cfg


def setup_logging(cfg: dict) -> None:
    lc = cfg.get("logging", {})
    level = getattr(logging, str(lc.get("level", "INFO")).upper(), logging.INFO)
    handlers: list = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    try:
        path = Path(lc.get("file", "logs/miner.log"))
        if not path.is_absolute():
            path = BASE_DIR / path
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                path,
                maxBytes=int(lc.get("max_size_mb", 10)) * 1024 * 1024,
                backupCount=int(lc.get("backup_count", 5)),
                encoding="utf-8",
            )
        )
    except OSError:
        pass
    logging.basicConfig(level=level, format="%(asctime)s - %(levelname)s - %(message)s", handlers=handlers)


def install_chromium(log: Callable[[str], None], timeout: int = 1800) -> bool:
    """Download Chromium via the bundled Playwright driver (needed on the first exe run)."""
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env

        drv = compute_driver_executable()
        cmd = list(drv) if isinstance(drv, (tuple, list)) else [str(drv)]
        env = get_driver_env()
    except Exception as e:
        log(f"❌ Не найден драйвер Playwright: {e}")
        return False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console window flashing in the exe
    try:
        proc = subprocess.run(
            cmd + ["install", "chromium"], env=env, capture_output=True, text=True,
            timeout=timeout, creationflags=flags,
        )
    except Exception as e:
        log(f"❌ Не удалось запустить установку Chromium: {e}")
        return False
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        log("❌ Установка Chromium не удалась: " + " | ".join(tail))
        return False
    return True


# --------------------------------------------------------------------------- #
# Win32: find / hide / show the Playwright Chromium window
# --------------------------------------------------------------------------- #
IS_WINDOWS = sys.platform == "win32"
SW_HIDE, SW_SHOW, SW_RESTORE = 0, 5, 9
GW_OWNER = 4
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
EVENT_OBJECT_SHOW = 0x8002
WINEVENT_OUTOFCONTEXT = 0x0000
PM_REMOVE = 0x0001
QS_ALLINPUT = 0x04FF
OFFSCREEN = -32000  # hidden windows are parked here, so a re-show by Chromium is invisible

if IS_WINDOWS:
    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    WINEVENTPROC = ctypes.WINFUNCTYPE(
        None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD
    )
    user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
    user32.EnumWindows.restype = wt.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
    user32.GetWindow.restype = wt.HWND
    user32.IsWindow.argtypes = [wt.HWND]
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsIconic.argtypes = [wt.HWND]
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.BringWindowToTop.argtypes = [wt.HWND]
    user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.SetWinEventHook.argtypes = [wt.DWORD, wt.DWORD, wt.HMODULE, WINEVENTPROC, wt.DWORD, wt.DWORD, wt.DWORD]
    user32.SetWinEventHook.restype = wt.HANDLE
    user32.UnhookWinEvent.argtypes = [wt.HANDLE]
    user32.UnhookWinEvent.restype = wt.BOOL
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.MsgWaitForMultipleObjects.argtypes = [wt.DWORD, ctypes.c_void_p, wt.BOOL, wt.DWORD, wt.DWORD]
    user32.MsgWaitForMultipleObjects.restype = wt.DWORD


class BrowserWindow:
    """Controls ALL windows of OUR Playwright Chromium.

    Chromium is launched with a unique `--vkminer-tag=<id>` switch (unknown switches
    are ignored by Chromium but stay visible in the process command line), so we can
    tell our windows from the user's own Chrome, which is never touched.

    "Hidden" is an *intent* (`want_hidden`). While it is set, three layers keep the
    browser invisible, so it never flashes:
      1. Concealing = park the window off-screen (-32000) AND SW_HIDE it. If Chromium
         re-shows it on its own, it re-appears off-screen: nothing visible.
      2. A WinEvent hook (EVENT_OBJECT_SHOW) conceals any new Chromium window within
         milliseconds of it being shown.
      3. A polling sweep (4x/s) as a safety net.
    """

    GUARD_INTERVAL_S = 0.25
    PID_RESCAN_S = 2.0

    def __init__(self, tag: str, log: Callable[[str], None]):
        self.tag = tag
        self.log = log
        self.hwnd: Optional[int] = None
        self.want_hidden = False
        self._last_scan = 0.0
        self._rescan_pids = True
        self._pids: set = set()
        self._pid_scan_at = 0.0
        self._saved: dict = {}  # hwnd -> (x, y, w, h) before it was parked off-screen
        self._guard_thread: Optional[threading.Thread] = None
        self._guard_stop = threading.Event()
        self._last_guard_log = 0.0
        self.hook_active = False

    # ---- discovery --------------------------------------------------------
    def reset(self) -> None:
        """Browser is being (re)launched: forget cached handle / pids."""
        self.hwnd = None
        self._last_scan = 0.0
        self._rescan_pids = True
        self._saved.clear()

    def _tagged_pids(self) -> set:
        pids: set = set()
        if psutil is None:
            return pids
        needle = f"--vkminer-tag={self.tag}"
        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                if any(needle in (a or "") for a in (p.info.get("cmdline") or [])):
                    pids.add(p.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return pids

    def _pids_cached(self, force: bool = False) -> set:
        now = time.monotonic()
        if force or self._rescan_pids or now - self._pid_scan_at > self.PID_RESCAN_S:
            self._pids = self._tagged_pids()
            self._pid_scan_at = now
            self._rescan_pids = not self._pids  # keep scanning until the browser exists
        return self._pids

    def refresh_pids(self) -> set:
        """Call right after the browser process starts, BEFORE its window is created."""
        return self._pids_cached(force=True)

    def _is_ours(self, hwnd, pids: set) -> bool:
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids or user32.GetWindow(hwnd, GW_OWNER):
            return False
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value == "Chrome_WidgetWin_1"

    def _rect(self, hwnd) -> tuple:
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        return r.left, r.top, max(0, r.right - r.left), max(0, r.bottom - r.top)

    def _top_windows(self, pids: set) -> list:
        """[(hwnd, area, visible)] of top-level Chromium windows owned by `pids`."""
        found: list = []

        def cb(hwnd, _lparam):
            try:
                if self._is_ours(hwnd, pids):
                    _x, _y, w, h = self._rect(hwnd)
                    found.append((hwnd, w * h, bool(user32.IsWindowVisible(hwnd))))
            except Exception:
                pass
            return True

        user32.EnumWindows(WNDENUMPROC(cb), 0)
        return found

    def find(self, force: bool = False) -> Optional[int]:
        """Main (biggest) browser window. Works for hidden windows too."""
        if not IS_WINDOWS:
            return None
        if self.hwnd and user32.IsWindow(self.hwnd):
            return self.hwnd
        self.hwnd = None
        now = time.monotonic()
        if not force and now - self._last_scan < 2.0:
            return None
        self._last_scan = now
        pids = self._pids_cached(force=True)
        if not pids:
            return None
        wins = self._top_windows(pids)
        if wins:
            self.hwnd = max(wins, key=lambda w: w[1])[0]
            self.log(f"🪟 Окно браузера найдено (hwnd={self.hwnd})")
        return self.hwnd

    # ---- conceal / reveal -----------------------------------------------------
    def _conceal(self, hwnd) -> None:
        x, y, w, h = self._rect(hwnd)
        if x > OFFSCREEN // 2 and w > 0 and h > 0:  # remember where it was (not if already parked/minimized)
            self._saved[hwnd] = (x, y, w, h)
        user32.SetWindowPos(hwnd, None, OFFSCREEN, OFFSCREEN, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        user32.ShowWindow(hwnd, SW_HIDE)

    def _default_rect(self) -> tuple:
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        w, h = min(1280, max(640, sw - 80)), min(800, max(480, sh - 120))
        return (sw - w) // 2, (sh - h) // 2, w, h

    def _reveal(self, hwnd) -> None:
        x, y, w, h = self._saved.get(hwnd) or self._default_rect()
        user32.SetWindowPos(hwnd, None, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE)
        user32.ShowWindow(hwnd, SW_SHOW)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)

    def is_hidden(self) -> bool:
        """The user's intent. Cheap: safe to call from GUI / tray threads."""
        return self.want_hidden

    def _hide_visible(self) -> int:
        n = 0
        for hwnd, _area, visible in self._top_windows(self._pids_cached()):
            if visible:
                self._conceal(hwnd)
                n += 1
        return n

    def hide(self) -> bool:
        """Hide now and keep it hidden. Returns False if there is no window yet
        (it will be concealed as soon as it appears)."""
        self.want_hidden = True
        if not IS_WINDOWS:
            return False
        self.start_guard()
        return self._hide_visible() > 0

    def show(self) -> bool:
        self.want_hidden = False
        if not IS_WINDOWS:
            return False
        hwnd = self.find(force=True)
        if not hwnd:
            return False
        self._reveal(hwnd)
        return True

    # ---- guard (hook + polling) ---------------------------------------------------
    def start_guard(self) -> None:
        if not IS_WINDOWS or (self._guard_thread and self._guard_thread.is_alive()):
            return
        self._guard_stop.clear()
        self._guard_thread = threading.Thread(target=self._guard_loop, daemon=True, name="window-guard")
        self._guard_thread.start()

    def stop_guard(self) -> None:
        self._guard_stop.set()

    def _note_rogue_window(self) -> None:
        now = time.monotonic()
        if now - self._last_guard_log > 30:  # don't spam the log
            self._last_guard_log = now
            self.log("🫥 Chromium попытался показать окно (перезапуск/новая вкладка/focus) — скрыто")

    def _on_win_event(self, _hook, _event, hwnd, id_object, id_child, _thread, _time) -> None:
        """WinEvent callback (runs in the guard thread, during message pumping)."""
        try:
            if not self.want_hidden or not hwnd or id_object != 0 or id_child != 0:
                return
            pids = self._pids  # cached; never scan processes inside the callback
            if pids and self._is_ours(hwnd, pids):
                self._conceal(hwnd)
                self._note_rogue_window()
        except Exception:
            pass

    def _install_hook(self):
        """Returns (hook_handle, callback) or (None, None) if hooking is unavailable."""
        try:
            proc = WINEVENTPROC(self._on_win_event)  # keep a reference or it gets GC'd
            hook = user32.SetWinEventHook(
                EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW, None, proc, 0, 0, WINEVENT_OUTOFCONTEXT
            )
            return (hook, proc) if hook else (None, None)
        except Exception:
            return None, None

    def _pump_messages(self, timeout_ms: int) -> None:
        """Sleep until a message arrives (or timeout) and dispatch it: this is what
        delivers WinEvent callbacks to this thread."""
        user32.MsgWaitForMultipleObjects(0, None, False, timeout_ms, QS_ALLINPUT)
        msg = wt.MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _sweep(self) -> None:
        if not self.want_hidden:
            return
        if self._hide_visible():
            self._note_rogue_window()

    def _guard_loop(self) -> None:
        hook, _proc = self._install_hook()
        self.hook_active = bool(hook)
        last_sweep = 0.0
        try:
            while not self._guard_stop.is_set():
                self._pump_messages(int(self.GUARD_INTERVAL_S * 1000))
                now = time.monotonic()
                if now - last_sweep >= self.GUARD_INTERVAL_S:
                    last_sweep = now
                    try:
                        self._sweep()
                    except Exception:
                        pass
        finally:
            if hook:
                try:
                    user32.UnhookWinEvent(hook)
                except Exception:
                    pass
            self.hook_active = False


# --------------------------------------------------------------------------- #
# Worker: all browser logic
# --------------------------------------------------------------------------- #
class VKMinerWorker:
    """Owns the asyncio loop thread and the Playwright browser."""

    def __init__(self, events: queue.Queue, config: dict):
        self.events = events
        self.cfg = config
        self.loop = asyncio.new_event_loop()
        self.loop_thread: Optional[threading.Thread] = None

        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

        self.stop_event = threading.Event()
        self.mining_active = False
        self.current_campaign: Optional[str] = None
        self.campaigns: list = []

        self.window = BrowserWindow(uuid.uuid4().hex[:12], self.log)
        self._mining_future = None
        self._refreshing = False
        self._shutting_down = False
        self._intent_initialised = False
        self._link_warned: set = set()
        self._page_crashed = False
        self.headless = bool(config.get("browser", {}).get("headless", False))

    # ---- helpers ----------------------------------------------------------
    def emit(self, kind: str, *args: Any) -> None:
        self.events.put((kind, *args))

    def log(self, message: str, level: int = logging.INFO) -> None:
        logger.log(level, message)
        self.emit("log", message)

    def _drops_cfg(self, key: str, default: Any) -> Any:
        return self.cfg.get("drops", {}).get(key, default)

    def _submit(self, coro, name: str):
        if self.loop.is_closed():
            coro.close()
            return None
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def _done(f):
            if f.cancelled():
                return
            exc = f.exception()
            if exc:
                logger.error("%s failed", name, exc_info=exc)
                self.emit("log", f"❌ {name}: {exc!r}")

        fut.add_done_callback(_done)
        return fut

    async def _sleep(self, seconds: float) -> None:
        """Sleep that wakes up quickly when stop is requested."""
        end = time.monotonic() + seconds
        while not self.stop_event.is_set():
            left = end - time.monotonic()
            if left <= 0:
                return
            await asyncio.sleep(min(0.5, left))

    async def _human_delay(self) -> None:
        lo = float(self._drops_cfg("claim_delay_min", 2))
        hi = float(self._drops_cfg("claim_delay_max", 5))
        await self._sleep(random.uniform(min(lo, hi), max(lo, hi)))

    # ---- public sync API (called from GUI / tray threads) -----------------
    def start(self) -> None:
        ready = threading.Event()

        def run_loop():
            asyncio.set_event_loop(self.loop)
            self.loop.call_soon(ready.set)
            self.loop.run_forever()

        self.loop_thread = threading.Thread(target=run_loop, daemon=True, name="worker-loop")
        self.loop_thread.start()
        ready.wait(5)
        self._submit(self._startup(), "startup")

    def request_refresh(self) -> None:
        if self.mining_active:
            self.log("⚠️ Идёт майнинг — сначала остановите его, чтобы обновить список.")
            return
        if self._refreshing:
            return
        self._submit(self._refresh_campaigns(), "refresh")

    def start_mining(self, campaigns: list) -> None:
        if self.mining_active:
            self.log("⚠️ Майнинг уже запущен.")
            return
        if self._refreshing:
            self.log("⚠️ Подождите, список кампаний ещё обновляется.")
            return
        self.stop_event.clear()
        self.mining_active = True
        self.emit("mining", True)
        self._mining_future = self._submit(self.run_mining(campaigns), "mining")

    def stop_mining(self) -> None:
        if not self.mining_active:
            return
        self.log("🛑 Остановка майнинга...")
        self.stop_event.set()
        fut = self._mining_future
        if fut is not None:
            fut.cancel()

    def browser_hidden(self) -> bool:
        return self.window.is_hidden()

    def toggle_browser(self) -> bool:
        """Hide the browser (and keep it hidden) or show it."""
        if self.headless:
            self.log("ℹ️ Режим headless: у браузера нет окна (browser.headless: true в config.yaml)")
            return False
        if self.window.is_hidden():
            ok = self.window.show()
            self.log("🪟 Браузер показан" if ok else "⚠️ Браузер ещё не запущен — он будет показан при запуске")
        else:
            ok = self.window.hide()
            self.log(
                "🫥 Браузер скрыт — майнинг продолжается в фоне"
                if ok else "🫥 Браузер будет скрыт сразу после запуска"
            )
        return True

    def show_browser(self) -> None:
        self.window.show()

    def shutdown(self, timeout: float = 10.0) -> None:
        self._shutting_down = True
        self.stop_event.set()
        self.window.stop_guard()
        if not self.loop.is_closed() and self.loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._shutdown_async(), self.loop)
            try:
                fut.result(timeout)
            except Exception as e:
                logger.warning("shutdown: %r", e)
            self.loop.call_soon_threadsafe(self.loop.stop)

    async def _shutdown_async(self) -> None:
        if self._mining_future is not None:
            self._mining_future.cancel()
            await asyncio.sleep(0.2)
        try:
            if self.context and self.page and not self.page.is_closed():
                if await self.page.locator(LOGGED_IN_SELECTOR).count() > 0:
                    await self.save_auth()
        except Exception:
            pass
        await self._close_browser()

    # ---- startup / browser lifecycle -------------------------------------
    async def _startup(self) -> None:
        self.log(f"🚀 {APP_NAME} v{__version__}")
        try:
            await self._launch_browser()
        except Exception as e:
            self.emit("busy", False)
            self.log(f"❌ Браузер не запущен: {e}", logging.ERROR)
            raise
        await self._refresh_campaigns()

    async def _launch_browser(self) -> None:
        self.window.reset()  # BEFORE the window exists, so the guard can hide it immediately
        if not self._intent_initialised:
            self._intent_initialised = True
            if self.cfg.get("browser", {}).get("start_hidden", False) or self.headless:
                self.window.want_hidden = True
        if not self.headless:
            self.window.start_guard()
        self.playwright = await async_playwright().start()
        await self._ensure_chromium()
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-gpu",
            "--autoplay-policy=no-user-gesture-required",
            f"--vkminer-tag={self.window.tag}",  # lets us find OUR window by PID
        ]
        if self.cfg.get("browser", {}).get("mute_audio", True):
            args.append("--mute-audio")
        if self.window.want_hidden and not self.headless:
            # Even the very first window is created off-screen: nothing ever flashes.
            args += [f"--window-position={OFFSCREEN},{OFFSCREEN}", "--window-size=1280,720"]

        if self.headless:
            try:  # "new" headless (real Chrome, no window) where available
                self.browser = await self.playwright.chromium.launch(channel="chromium", headless=True, args=args)
            except Exception:
                self.browser = await self.playwright.chromium.launch(headless=True, args=args)
        else:
            self.browser = await self.playwright.chromium.launch(headless=False, args=args)
            await asyncio.to_thread(self.window.refresh_pids)  # hook can now recognise the window at once
        self.browser.on("disconnected", lambda *_: self.log("⚠️ Процесс браузера завершился (будет перезапущен при следующем действии)"))

        ctx_kwargs: dict = {"no_viewport": not self.headless}
        if self.headless:
            ctx_kwargs["viewport"] = {"width": 1280, "height": 720}
            # headless builds may advertise "HeadlessChrome": present a normal Chrome UA
            ctx_kwargs["user_agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{self.browser.version} Safari/537.36"
            )
        if AUTH_FILE.exists():
            ctx_kwargs["storage_state"] = str(AUTH_FILE)
        try:
            self.context = await self.browser.new_context(**ctx_kwargs)
        except Exception as e:
            self.log(f"⚠️ vk_auth.json не загружен ({e}) — запускаю без сохранённой сессии")
            ctx_kwargs.pop("storage_state", None)
            self.context = await self.browser.new_context(**ctx_kwargs)
        await self.context.add_init_script(VISIBILITY_SPOOF_JS)
        # Diagnostics: if a window ever appears by itself, the log says why.
        self.context.on("page", lambda p: self.log(f"🆕 Chromium открыл вкладку/окно: {p.url or 'about:blank'}"))
        self._page_crashed = False
        self.page = await self._new_page()

        if self.headless:
            self.log("✅ Браузер запущен в фоновом режиме (headless, без окна)")
            return
        for _ in range(24):  # wait for the OS window to appear
            if await asyncio.to_thread(self.window.find, True):
                break
            await asyncio.sleep(0.25)
        if self.window.want_hidden:
            self.window.hide()
        hook = "хук окна активен" if self.window.hook_active else "хук недоступен, работает проверка 4 раза/сек"
        self.log(f"✅ Браузер запущен ({'скрыт' if self.window.want_hidden else 'виден'}; {hook})")

    async def _ensure_chromium(self) -> None:
        """First run (esp. the exe): download Chromium if it is not installed yet."""
        try:
            if Path(self.playwright.chromium.executable_path).exists():
                return
        except Exception:
            return
        self.log("⬇️ Первый запуск: скачиваю браузер Chromium (~150 МБ). Это займёт несколько минут, окно не закрывайте...")
        self.emit("busy", True)
        ok = await asyncio.to_thread(install_chromium, self.log)
        if not ok:
            raise RuntimeError("Chromium не установлен. Проверьте интернет и перезапустите программу.")
        self.log("✅ Chromium установлен")

    async def _new_page(self):
        page = await self.context.new_page()

        def on_crash():
            self.log("💥 Вкладка браузера упала — открою новую при следующем действии", logging.WARNING)
            if page is self.page:
                self._page_crashed = True

        page.on("crash", lambda *_: on_crash())
        return page

    async def _close_browser(self) -> None:
        for obj in (self.context, self.browser):
            try:
                if obj:
                    await obj.close()
            except Exception:
                pass
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass
        self.context = self.browser = self.page = self.playwright = None

    async def ensure_browser(self) -> bool:
        """Make sure there is a live browser + page (the user may have closed it)."""
        try:
            if self.browser and self.browser.is_connected():
                if self.page is None or self.page.is_closed() or self._page_crashed:
                    old_page = self.page
                    self.page = await self._new_page()  # new tab first, so the window never disappears
                    self._page_crashed = False
                    if old_page is not None and not old_page.is_closed():
                        try:
                            await old_page.close()
                        except Exception:
                            pass
                return True
            self.log("🔄 Браузер был закрыт — перезапускаю...")
            await self._close_browser()
            await self._launch_browser()
            return True
        except Exception as e:
            self.log(f"❌ Не удалось запустить браузер: {e}", logging.ERROR)
            return False

    async def _goto(self, url: str, timeout: int = 30000, settle_ms: int = 2000) -> bool:
        for attempt in range(3):
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                if settle_ms:
                    await self.page.wait_for_timeout(settle_ms)
                return True
            except Exception as e:
                if attempt < 2:
                    await self.page.wait_for_timeout(1000)
                    continue
                self.log(f"❌ Не удалось открыть {url}: {e}", logging.ERROR)
        return False

    # ---- auth -------------------------------------------------------------
    async def check_login(self) -> bool:
        page = self.page
        logged = page.locator(LOGGED_IN_SELECTOR).first
        login = page.locator(LOGIN_BUTTON_SELECTOR).first
        for attempt in range(2):  # the header may render late: give it a second chance
            try:
                await logged.or_(login).wait_for(timeout=15000)
            except Exception:
                pass
            if await logged.count() > 0:
                return True
            if await login.count() > 0:
                break
            if attempt == 0:
                await page.wait_for_timeout(3000)
        if await login.count() > 0:
            if self.headless:
                self.log("🔑 Вы не авторизованы. Запустите программу с browser.headless: false, войдите в VK "
                         "(сессия сохранится в vk_auth.json), затем верните headless: true.", logging.WARNING)
            else:
                self.log("🔑 Вы не авторизованы. Войдите в VK в окне браузера и нажмите «Обновить».", logging.WARNING)
                self.window.show()  # a hidden browser would make logging in impossible
        else:
            # Inconclusive (page not loaded?) — do NOT pop the browser up for that.
            self.log("⚠️ Не удалось определить, авторизованы ли вы (страница не догрузилась). Нажмите «Обновить».", logging.WARNING)
        return False

    async def save_auth(self) -> None:
        try:
            await self.context.storage_state(path=str(AUTH_FILE))
        except Exception as e:
            self.log(f"⚠️ Не удалось сохранить сессию: {e}", logging.WARNING)

    # ---- campaigns --------------------------------------------------------
    async def _refresh_campaigns(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self.emit("busy", True)
        try:
            campaigns = await self.fetch_campaigns()
            self.emit("campaigns", campaigns)
        finally:
            self._refreshing = False
            self.emit("busy", False)

    async def _section_cards(self, section: str, cards_wait_ms: int = 2500):
        """Locator for the cards of one section ('Активные', 'Доступные', ...)."""
        page = self.page
        try:
            await page.wait_for_selector(SECTION_TITLE_SELECTOR, timeout=15000)
        except Exception:
            return None
        headers = page.locator(SECTION_TITLE_SELECTOR)
        for i in range(await headers.count()):
            header = headers.nth(i)
            if section in (await header.inner_text()):
                lst = header.locator(
                    'xpath=following-sibling::*[contains(@class, "DropCampaignsList_root_")][1]'
                )
                cards = lst.locator(CAMPAIGN_CARD_SELECTOR)
                try:  # wait until the list is rendered (returns at once when it already is)
                    await cards.first.wait_for(state="attached", timeout=cards_wait_ms)
                except Exception:
                    pass  # empty section
                return cards
        return None

    async def fetch_campaigns(self) -> list:
        if not await self.ensure_browser():
            return []
        self.log("📋 Загружаю кампании...")
        if not await self._goto(CAMPAIGNS_URL):
            return []
        if not await self.check_login():
            return []
        await self.save_auth()
        if DEBUG_DUMP:
            await self._dump_debug("campaigns")

        date_re = re.compile(r"\d{1,2}\s+[а-яё]+\s+\d{1,2}:\d{2}", re.I)
        blacklist = {str(g).lower() for g in self._drops_cfg("blacklist_games", [])}
        preferred = [str(g).lower() for g in self._drops_cfg("preferred_games", [])]

        campaigns: list = []
        for section in MINEABLE_SECTIONS:
            cards = await self._section_cards(section)
            if cards is None:
                self.log(f"⚠️ Раздел «{section}» не найден")
                continue
            for i in range(await cards.count()):
                try:
                    text = await cards.nth(i).inner_text()
                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    if len(lines) < 2 or len(lines[0]) < 3:
                        continue
                    name, game = lines[0], lines[1]
                    dates = next((l for l in lines if date_re.search(l)), "")
                    if game.lower() in blacklist:
                        self.log(f"  🚫 {name} ({game}) — в чёрном списке")
                        continue
                    campaigns.append(
                        {
                            "key": f"{section}|{name}|{dates}",
                            "name": name,
                            "game": game,
                            "dates": dates,
                            "section": section,
                            "card_index": i,
                        }
                    )
                    self.log(f"  ✅ [{section}] {name} ({game}) — {dates}")
                except Exception as e:
                    self.log(f"⚠️ Ошибка разбора карточки {i} в «{section}»: {e}", logging.WARNING)

        if preferred:  # stable sort: preferred games first
            campaigns.sort(key=lambda c: 0 if c["game"].lower() in preferred else 1)
        self.campaigns = campaigns
        self.log(f"✅ Кампаний загружено: {len(campaigns)}")
        return campaigns

    async def _open_campaign_card(self, campaign: dict) -> bool:
        """Open the campaigns page and expand the card of this campaign."""
        if not await self._goto(CAMPAIGNS_URL, settle_ms=0):
            return False
        cards = await self._section_cards(campaign["section"], cards_wait_ms=8000)
        if cards is None:
            self.log(f"❌ Раздел «{campaign['section']}» не найден", logging.WARNING)
            return False

        def matches(text: str) -> bool:
            return campaign["name"] in text and (not campaign["dates"] or campaign["dates"] in text)

        total = await cards.count()
        card = None
        idx = campaign["card_index"]
        if idx < total and matches(await cards.nth(idx).inner_text()):
            card = cards.nth(idx)
        else:
            for i in range(total):
                if matches(await cards.nth(i).inner_text()):
                    card = cards.nth(i)
                    break
        if card is None:
            self.log(f"❌ Карточка «{campaign['name']}» не найдена (кампания могла закончиться)", logging.WARNING)
            return False
        await card.click()
        return True  # callers wait for the details (e.g. the «Смотреть стрим» button) themselves

    # ---- stream / player ---------------------------------------------------
    async def _player_state(self) -> dict:
        return await self.page.evaluate(PLAYER_STATE_JS)

    async def check_if_stream_is_live(self) -> bool:
        try:
            st = await self._player_state()
            if st.get("playing"):
                self.log(f"✅ Стрим идёт (state={st.get('state')}, online={st.get('online')})")
                return True
            self.log(f"❌ Стрим не воспроизводится (state={st.get('state')}, player={st.get('present')})")
            return False
        except Exception as e:
            self.log(f"⚠️ Ошибка проверки стрима: {e}", logging.WARNING)
            return False

    async def _try_play(self) -> None:
        try:
            await self.page.evaluate(TRY_PLAY_JS)
        except Exception:
            pass

    async def _jiggle(self) -> None:
        try:
            await self.page.mouse.move(random.randint(100, 700), random.randint(100, 400))
        except Exception:
            pass

    async def _visible(self, text: str, exact: bool = False) -> bool:
        loc = self.page.get_by_text(text, exact=exact).locator("visible=true")
        return await loc.count() > 0

    async def _dismiss_popups(self) -> None:
        page = self.page
        for _ in range(3):
            ok = page.get_by_text("Понятно", exact=True).locator("visible=true")
            if await ok.count() == 0:
                break
            try:
                await ok.first.click(timeout=3000)
            except Exception:
                break
            await page.wait_for_timeout(500)
        try:
            await page.keyboard.press("Escape")
            await page.mouse.move(400, 300)  # move away so the ring tooltip closes
        except Exception:
            pass

    async def _dump_debug(self, tag: str) -> None:
        """Save page HTML (+ screenshot) to debug/ — attach these when reporting a problem."""
        try:
            DEBUG_DIR.mkdir(exist_ok=True)
            base = DEBUG_DIR / f"{tag}_{time.strftime('%Y%m%d_%H%M%S')}"
            Path(str(base) + ".html").write_text(await self.page.content(), encoding="utf-8")
            try:
                await self.page.screenshot(path=str(base) + ".png", timeout=5000)
            except Exception:
                pass  # a hidden window may not render frames; HTML is enough
            self.log(f"🧪 Отладочные файлы: debug/{base.name}.html (+ .png)")
        except Exception as e:
            self.log(f"⚠️ Не удалось сохранить отладку: {e}", logging.WARNING)

    # ---- drops: widget + claiming -----------------------------------------
    async def _read_drop_widget(self) -> dict:
        return await self.page.evaluate(DROP_WIDGET_JS)

    async def claim_reward(self) -> str:
        """Click the 100% ring / box icon and confirm the reward.

        Returns: 'ok' | 'need_link' | 'error' | 'unknown'
        """
        page = self.page
        await self._human_delay()
        pt = await page.evaluate(DROP_WIDGET_POINT_JS)
        if not pt:
            return "unknown"
        try:
            await page.mouse.move(pt["x"], pt["y"])
            await page.mouse.click(pt["x"], pt["y"])
        except Exception as e:
            self.log(f"⚠️ Не удалось нажать на кнопку награды: {e}", logging.WARNING)
            await self._dump_debug("claim_click_failed")
            return "error"

        status = "unknown"
        quiet = 0
        for _ in range(8):
            await page.wait_for_timeout(1000)
            if await self._visible("Не подключен VK"):
                status = "need_link"
                break
            if await self._visible("Ошибка получения"):
                status = "error"
                break
            if await self._visible("Награда получена"):
                status = "ok"
                break
            claim = page.get_by_text("Получить", exact=True).locator("visible=true")
            if await claim.count() > 0:
                quiet = 0
                await self._human_delay()
                try:
                    await claim.first.click(timeout=3000)
                except Exception:
                    pass
                continue
            # No popup / button: if the widget is no longer claimable, the click itself
            # claimed the reward (two quiet checks in a row to avoid flicker).
            w = await self._read_drop_widget()
            quiet = quiet + 1 if w.get("kind") != "claimable" else 0
            if quiet >= 2:
                status = "ok"
                break

        await self._dismiss_popups()
        if status != "ok":
            await self._dump_debug(f"claim_{status}")
        return status

    async def _claim_visible_buttons(self) -> int:
        """On the (expanded) campaign card: press every 'Получить' button."""
        page = self.page
        claimed = 0
        for _ in range(10):
            btn = page.get_by_text("Получить", exact=True).locator("visible=true")
            if await btn.count() == 0:
                break
            await self._human_delay()
            try:
                await btn.first.click(timeout=3000)
            except Exception:
                break
            await page.wait_for_timeout(2000)
            if await self._visible("Ошибка получения"):
                self.log("⚠️ Ошибка при получении награды на странице кампаний", logging.WARNING)
                await self._dismiss_popups()
                break
            claimed += 1
            await self._dismiss_popups()
        if claimed:
            self.log(f"🎁 На странице кампаний получено наград: {claimed}")
        return claimed

    async def sweep_campaign_claims(self, campaign: dict) -> int:
        """Open the campaign card and claim everything that is already at 100%."""
        if not await self._open_campaign_card(campaign):
            return 0
        return await self._claim_visible_buttons()

    # ---- mining -----------------------------------------------------------
    async def start_campaign_mining(self, campaign: dict) -> str:
        """Open the campaign, press 'Смотреть стрим' and wait (briefly) for the player.

        Returns: 'ok' | 'offline' (streamer is not live) | 'no_stream' | 'failed'
        """
        if not await self.ensure_browser():
            return "failed"
        page = self.page
        try:
            if not await self._open_campaign_card(campaign):
                return "failed"

            if campaign["key"] not in self._link_warned:  # warn once, not on every retry
                for label in LINK_ACCOUNT_BUTTONS:
                    if await page.locator(f'button:has-text("{label}")').locator("visible=true").count() > 0:
                        self._link_warned.add(campaign["key"])
                        self.log(f"⚠️ Требуется «{label}» — без привязки аккаунта награду не получить.", logging.WARNING)

            # Collect rewards that are already at 100% before we start watching
            await self._claim_visible_buttons()

            watch_btn = page.locator(WATCH_BUTTON_SELECTOR).first
            try:
                await watch_btn.wait_for(state="visible", timeout=8000)
            except Exception:
                self.log("❌ Кнопка «Смотреть стрим» не найдена", logging.WARNING)
                return "failed"

            pages_before = set(self.context.pages)
            await watch_btn.click(force=True)

            # 1) wait for the navigation to a stream (poll fast, bail out on the server error text)
            deadline = time.monotonic() + 15
            while "/app/box/campaigns" in page.url or page.is_closed():
                if self.stop_event.is_set():
                    return "failed"
                new_pages = [p for p in self.context.pages if p not in pages_before]
                if new_pages:  # the stream opened in a new tab
                    old, self.page = page, new_pages[-1]
                    page = self.page
                    try:
                        await old.close()
                    except Exception:
                        pass
                    break
                if await self._visible(NO_STREAMS_TEXT):
                    self.log("📭 Сейчас нет подходящих трансляций для этой кампании")
                    return "no_stream"
                if time.monotonic() > deadline:
                    self.log("❌ Переход на стрим не произошёл", logging.WARNING)
                    return "failed"
                await page.wait_for_timeout(300)

            # 2) wait for playback; give up at once if the streamer is not live
            start_timeout = float(self._drops_cfg("stream_start_timeout_seconds", 15))
            offline_after = float(self._drops_cfg("offline_check_seconds", 4))
            t0 = time.monotonic()
            not_live_since: Optional[float] = None
            tried_play = 0
            while time.monotonic() - t0 < start_timeout:
                if self.stop_event.is_set():
                    return "failed"
                st = await self._player_state()
                if st.get("playing"):
                    self.log(f"✅ Стрим идёт: {page.url}")
                    return "ok"
                now = time.monotonic()
                if st.get("panel") and not st.get("online"):
                    not_live_since = not_live_since or now
                    if offline_after > 0 and now - not_live_since >= offline_after:
                        self.log(
                            f"💤 Стример не в эфире — пробую другой стрим "
                            f"(state={st.get('state')}, online={st.get('online')})"
                        )
                        return "offline"
                else:
                    not_live_since = None
                elapsed = now - t0
                if tried_play == 0 and elapsed > 2.5 or tried_play == 1 and elapsed > 7:
                    tried_play += 1
                    await self._try_play()
                await page.wait_for_timeout(400)
            self.log("💤 Стрим не начал воспроизводиться — пробую другой", logging.INFO)
            return "failed"
        except Exception as e:
            self.log(f"❌ Ошибка запуска майнинга: {e}", logging.ERROR)
            return "failed"

    async def watch_loop(self, campaign: dict) -> str:
        """Watch the stream, track progress, claim rewards.

        Returns: 'finished' | 'idle' | 'restart' | 'failed' | 'stopped'
        """
        key, name = campaign["key"], campaign["name"]
        poll = min(120.0, max(5.0, float(self._drops_cfg("check_interval_seconds", 30))))
        stall_limit = max(1.0, float(self._drops_cfg("stall_timeout_minutes", 10))) * 60

        t0 = time.monotonic()
        last_percent: Optional[float] = None
        last_change = t0
        last_auth_save = t0
        seen_widget = False
        just_claimed = False
        none_streak = 0
        dead_streak = 0
        errors = 0
        claim_failures = 0
        claims = 0

        self.log(f"⛏ Майнинг «{name}» запущен")
        while not self.stop_event.is_set():
            try:
                await self._jiggle()

                # 1) is the stream still playing? (when it is not, re-check every few seconds)
                st = await self._player_state()
                if st.get("playing"):
                    dead_streak = 0
                else:
                    dead_streak += 1
                    await self._try_play()
                    if dead_streak >= 3:
                        self.log("⚠️ Стрим остановился/закончился — ищу другой...")
                        return "restart"
                    await self._sleep(4)
                    continue

                # 2) drop widget
                w = await self._read_drop_widget()
                kind = w.get("kind")
                now = time.monotonic()

                if kind == "claimable":
                    seen_widget, none_streak = True, 0
                    self.emit("progress", key, 100.0)
                    self.log("🎯 Прогресс 100% — забираю награду...")
                    status = await self.claim_reward()
                    if status == "ok":
                        claims += 1
                        claim_failures = 0
                        just_claimed = True
                        last_change = time.monotonic()
                        self.emit("claimed", key)
                        self.log(f"🎁 Награда получена (за сессию: {claims}). Проверяю, есть ли ещё награды...")
                    elif status == "need_link":
                        self.log("❌ Нужно подключить VK ID / VK Play в аккаунте — награду забрать нельзя.", logging.ERROR)
                        return "failed"
                    else:
                        claim_failures += 1
                        self.log(f"⚠️ Не удалось забрать награду ({status}), попытка {claim_failures}/3", logging.WARNING)
                        if claim_failures >= 3:
                            return "failed"
                    await self._sleep(5)
                    continue

                if kind == "ring":
                    seen_widget, none_streak = True, 0
                    percent = float(w.get("percent") or 0)
                    self.emit("progress", key, percent)
                    if last_percent is None or percent != last_percent:
                        self.log(f"📈 «{name}»: {percent:.0f}%")
                        last_change = now
                    if just_claimed:
                        self.log(f"➕ Есть следующая награда ({percent:.0f}%) — продолжаю майнинг")
                        just_claimed = False
                    last_percent = percent
                    if now - last_change > stall_limit:
                        self.log(f"⚠️ Прогресс не растёт {stall_limit / 60:.0f} мин — переоткрываю стрим", logging.WARNING)
                        return "restart"
                else:  # 'none' / 'nofooter'
                    none_streak += 1
                    if seen_widget and none_streak >= 3:
                        self.log("🔎 Виджет прогресса пропал — проверяю страницу кампаний на оставшиеся награды...")
                        swept = await self.sweep_campaign_claims(campaign)
                        if swept > 0:
                            claims += swept
                            self.emit("claimed", key)
                            return "restart"  # continue mining after claiming leftovers
                        self.log(f"🏁 Все награды кампании «{name}» получены (за сессию: {claims})")
                        self.emit("progress", key, 100.0)
                        return "finished"
                    if not seen_widget and now - t0 >= IDLE_TIMEOUT_S:
                        self.log("ℹ️ На этом стриме нет прогресса дропов", logging.WARNING)
                        return "idle"

                if now - last_auth_save > AUTH_SAVE_EVERY_S:
                    last_auth_save = now
                    await self.save_auth()

                errors = 0
                await self._sleep(10 if (just_claimed or none_streak) else poll)

            except Exception as e:
                errors += 1
                self.log(f"⚠️ Ошибка в цикле майнинга ({errors}/5): {e}", logging.WARNING)
                if errors >= 5 or (self.page is None or self.page.is_closed()):
                    return "restart"
                await self._sleep(5)
        return "stopped"

    async def mine_campaign(self, campaign: dict) -> str:
        """Mine one campaign until all its rewards are claimed.

        If «Смотреть стрим» lands on a stream that is not live, we just press it again
        (quickly, many times) — the button only works when a live stream exists.

        Returns: 'done' | 'no_progress' | 'failed' | 'stopped'
        """
        self.log(f"📺 Кампания: {campaign['name']} ({campaign['game']})")
        max_attempts = max(1, int(self._drops_cfg("max_start_attempts", 100)))
        start_failures = idle_restarts = rounds = 0
        while not self.stop_event.is_set():
            rounds += 1
            if rounds > MAX_ROUNDS:
                self.log("⚠️ Слишком много перезапусков стрима, пропускаю кампанию", logging.WARNING)
                return "failed"

            status = await self.start_campaign_mining(campaign)
            if status == "ok":
                start_failures = 0
                result = await self.watch_loop(campaign)
                if result == "finished":
                    return "done"
                if result == "failed":
                    return "failed"
                if result == "stopped":
                    break
                if result == "idle":
                    idle_restarts += 1
                    if idle_restarts >= 2:
                        self.log(
                            "ℹ️ Прогресс не появился на двух стримах подряд — вероятно, все награды "
                            "уже получены или каналы не участвуют. Перехожу дальше."
                        )
                        return "no_progress"
                await self._sleep(1)  # 'restart' -> open the campaign again (another random stream)
            else:
                start_failures += 1
                limit = MAX_NO_STREAM_ATTEMPTS if status == "no_stream" else max_attempts
                if start_failures >= limit:
                    self.log(f"❌ Не удалось запустить стрим {start_failures} раз подряд — пропускаю кампанию", logging.WARNING)
                    return "failed"
                if status != "no_stream":
                    self.log(f"🔁 Повторная попытка запуска стрима ({start_failures}/{limit})")
                # no_stream = the server really has no live streams: wait; otherwise retry at once
                await self._sleep(30 if status == "no_stream" else 0.5)
        return "stopped"

    async def run_mining(self, campaigns: list) -> None:
        current_key = None
        try:
            if not await self.ensure_browser():
                return
            if not await self._goto(CAMPAIGNS_URL) or not await self.check_login():
                return
            for campaign in campaigns:
                if self.stop_event.is_set():
                    break
                current_key = campaign["key"]
                self.current_campaign = campaign["name"]
                self.emit("status", current_key, "⛏ майнинг")
                result = await self.mine_campaign(campaign)
                self.emit("status", current_key, STATUS_TEXT.get(result, result))
                current_key = None
                if result == "stopped":
                    break
            self.log("🏁 Майнинг завершён")
        except asyncio.CancelledError:
            if current_key:
                self.emit("status", current_key, STATUS_TEXT["stopped"])
            self.log("⏹ Майнинг остановлен")
            raise
        finally:
            self.mining_active = False
            self.current_campaign = None
            self.emit("mining", False)
            if not self._shutting_down:
                try:
                    if self.page and not self.page.is_closed():
                        await self.page.goto(CAMPAIGNS_URL, wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass


# --------------------------------------------------------------------------- #
# Tray icon (optional: pip install pystray pillow)
# --------------------------------------------------------------------------- #
class TrayIcon:
    def __init__(self, is_browser_hidden: Callable[[], bool], on_toggle_browser: Callable[[], None],
                 is_app_hidden: Callable[[], bool], on_toggle_app: Callable[[], None],
                 on_quit: Callable[[], None]):
        self._is_hidden = is_browser_hidden
        self._toggle = on_toggle_browser
        self._is_app_hidden = is_app_hidden
        self._toggle_app = on_toggle_app
        self._quit = on_quit
        self.icon = None

    @staticmethod
    def _make_image():
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(0, 119, 255, 255))
        d.polygon([(25, 19), (25, 45), (47, 32)], fill=(255, 255, 255, 255))
        return img

    def start(self) -> bool:
        if pystray is None:
            return False
        menu = pystray.Menu(
            pystray.MenuItem(
                lambda item: "Показать браузер" if self._is_hidden() else "Скрыть браузер",
                lambda icon, item: self._toggle(),
                default=True,
            ),
            pystray.MenuItem(
                lambda item: "Показать окно программы" if self._is_app_hidden() else "Скрыть окно программы",
                lambda icon, item: self._toggle_app(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda icon, item: self._quit()),
        )
        self.icon = pystray.Icon("vk_drops_miner", self._make_image(), "VK Live Drops Miner", menu)
        self.icon.run_detached()
        return True

    def notify(self, title: str, message: str) -> None:
        try:
            if self.icon:
                self.icon.notify(message, title)
        except Exception:
            pass

    def stop(self) -> None:
        try:
            if self.icon:
                self.icon.stop()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# GUI: only UI logic (never touches Playwright; reads events from a queue)
# --------------------------------------------------------------------------- #
class VKDropsMinerGUI(ctk.CTk):
    MAX_LOG_LINES = 2000

    def __init__(self, config: dict):
        super().__init__()
        self.title(f"{APP_NAME} v{__version__}")
        self.geometry("1000x750")
        self.minsize(900, 650)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.events: queue.Queue = queue.Queue()
        self.cfg = config
        self.worker = VKMinerWorker(self.events, config)

        self.campaign_data: list = []
        self.campaign_checkboxes: dict = {}
        self.campaign_progress: dict = {}
        self.campaign_percent: dict = {}
        self.campaign_status: dict = {}
        self.claim_counts: dict = {}
        self._tick = 0
        self._closing = False
        self._busy = False
        self._mining = False
        self._app_hidden = False
        self._tray_ok = False

        self._setup_ui()
        if self.worker.headless:
            self.btn_toggle_browser.configure(text="🌐 Headless (без окна)", state="disabled")
        self.protocol("WM_DELETE_WINDOW", self._on_closing)

        self.tray = TrayIcon(
            is_browser_hidden=self.worker.browser_hidden,
            on_toggle_browser=self.worker.toggle_browser,                 # thread-safe (pure Win32)
            is_app_hidden=lambda: self._app_hidden,
            on_toggle_app=lambda: self.events.put(("toggle_app",)),       # marshalled to the GUI thread
            on_quit=lambda: self.events.put(("quit",)),
        )
        self._tray_ok = self.tray.start()
        if self._tray_ok:
            self.log("🔔 Иконка в трее активна: двойной клик — скрыть/показать браузер, правый клик — меню")
        else:
            self.log("ℹ️ Для иконки в трее: pip install pystray pillow (без неё «Скрыть программу» просто сворачивает окно)")

        self._poll_events()
        self.worker.start()
        if self._tray_ok and config.get("gui", {}).get("start_in_tray", False):
            self.after(300, self._hide_app)

    # ---- UI setup ---------------------------------------------------------
    def _setup_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        top_panel = ctk.CTkFrame(self, height=60)
        top_panel.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 5))
        top_panel.grid_propagate(False)

        self.btn_refresh = ctk.CTkButton(top_panel, text="🔄 Обновить", width=110, command=self.on_refresh)
        self.btn_refresh.pack(side="left", padx=5, pady=10)

        self.btn_toggle_browser = ctk.CTkButton(
            top_panel, text="🌐 Скрыть браузер", width=170, command=self.on_toggle_browser
        )
        self.btn_toggle_browser.pack(side="left", padx=5, pady=10)

        self.btn_hide_app = ctk.CTkButton(
            top_panel, text="🗕 Скрыть программу", width=170, command=self.on_hide_app
        )
        self.btn_hide_app.pack(side="left", padx=5, pady=10)

        self.campaigns_frame = ctk.CTkScrollableFrame(self, label_text="Кампании (Активные и Доступные)")
        self.campaigns_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=5)
        self.campaigns_frame.grid_columnconfigure(0, weight=1)

        log_panel = ctk.CTkFrame(self)
        log_panel.grid(row=1, column=1, sticky="nsew", padx=10, pady=5)
        log_panel.grid_columnconfigure(0, weight=1)
        log_panel.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(log_panel, text="Логи", anchor="w").grid(row=0, column=0, sticky="ew", padx=5, pady=5)
        self.log_textbox = ctk.CTkTextbox(log_panel, height=300, font=("Consolas", 11))
        self.log_textbox.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)

        bottom_panel = ctk.CTkFrame(self, height=40)
        bottom_panel.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=5)
        bottom_panel.grid_propagate(False)

        self.btn_start_mining = ctk.CTkButton(
            bottom_panel, text="🚀 Запустить майнинг", width=180, command=self.on_start_mining
        )
        self.btn_start_mining.pack(side="left", padx=5, pady=5)
        self.btn_stop_mining = ctk.CTkButton(
            bottom_panel, text="⏹ Остановить", width=120, command=self.on_stop_mining, state="disabled"
        )
        self.btn_stop_mining.pack(side="left", padx=5, pady=5)

    # ---- event pump (the ONLY place where worker -> GUI data is applied) ---
    def _poll_events(self):
        try:
            for _ in range(300):
                self._handle_event(self.events.get_nowait())
                if self._closing:
                    return
        except queue.Empty:
            pass
        self._tick += 1
        if self._tick % 5 == 0:
            self._sync_browser_button()
        self.after(100, self._poll_events)

    def _handle_event(self, ev: tuple):
        kind = ev[0]
        if kind == "log":
            self._append_log(ev[1])
        elif kind == "campaigns":
            self.update_campaign_list(ev[1])
        elif kind == "progress":
            self._set_progress(ev[1], ev[2])
        elif kind == "status":
            self._set_status(ev[1], ev[2])
        elif kind == "claimed":
            self.claim_counts[ev[1]] = self.claim_counts.get(ev[1], 0) + 1
            self._set_status(ev[1], None)
            if self._tray_ok and self.cfg.get("gui", {}).get("show_notifications", True):
                name = next((c["name"] for c in self.campaign_data if c["key"] == ev[1]), "")
                self.tray.notify("🎁 Награда получена", name)
        elif kind == "mining":
            self._mining = bool(ev[1])
            self._update_buttons()
        elif kind == "busy":
            self._busy = bool(ev[1])
            self._update_buttons()
        elif kind == "show_app":
            self._show_app()
        elif kind == "hide_app":
            self._hide_app()
        elif kind == "toggle_app":
            self._show_app() if self._app_hidden else self._hide_app()
        elif kind == "quit":
            self._on_closing()

    def _update_buttons(self):
        self.btn_start_mining.configure(state="disabled" if (self._mining or self._busy) else "normal")
        self.btn_stop_mining.configure(state="normal" if self._mining else "disabled")
        self.btn_refresh.configure(state="disabled" if (self._mining or self._busy) else "normal")

    def _sync_browser_button(self):
        if self.worker.headless:
            return
        text = "🌐 Показать браузер" if self.worker.browser_hidden() else "🌐 Скрыть браузер"
        if self.btn_toggle_browser.cget("text") != text:
            self.btn_toggle_browser.configure(text=text)

    def _append_log(self, msg: str):
        self.log_textbox.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        lines = int(self.log_textbox.index("end-1c").split(".")[0])
        if lines > self.MAX_LOG_LINES:
            self.log_textbox.delete("1.0", f"{lines - self.MAX_LOG_LINES}.0")
        self.log_textbox.see("end")

    def log(self, message: str):
        self.events.put(("log", message))

    # ---- handlers -----------------------------------------------------------
    def on_refresh(self):
        self.log("🔄 Обновляю список кампаний...")
        self.worker.request_refresh()

    def on_hide_app(self):
        self._hide_app()

    def _hide_app(self):
        if self._tray_ok:
            self.withdraw()  # gone from screen and taskbar; restore from the tray icon
            self._app_hidden = True
            self.log("🫥 Программа скрыта в трей (иконка в трее → «Показать окно программы»)")
        else:
            self.iconify()  # no tray available: a withdrawn window could never come back
            self.log("ℹ️ Трей недоступен (pip install pystray pillow) — окно просто свёрнуто")

    def _show_app(self):
        self.deiconify()
        self.lift()
        self.focus_force()
        self._app_hidden = False

    def on_toggle_browser(self):
        self.worker.toggle_browser()
        self._sync_browser_button()

    def on_start_mining(self):
        selected = [c for c in self.campaign_data if self.campaign_checkboxes[c["key"]].get() == 1]
        if not selected:
            self.log("⚠️ Не выбрано ни одной кампании!")
            return
        for c in selected:
            self._set_progress(c["key"], 0)
            self.claim_counts[c["key"]] = 0
            self._set_status(c["key"], "в очереди")
        self.log(f"🚀 Запускаю майнинг: кампаний — {len(selected)}")
        self.worker.start_mining(selected)

    def on_stop_mining(self):
        self.worker.stop_mining()

    # ---- campaign list ------------------------------------------------------
    def _set_progress(self, key: str, percent: float):
        pb, pl = self.campaign_progress.get(key), self.campaign_percent.get(key)
        if pb is None or pl is None:
            return
        pb.set(max(0.0, min(1.0, percent / 100.0)))
        pl.configure(text=f"{percent:.0f}%")

    def _set_status(self, key: str, text: Optional[str]):
        lbl = self.campaign_status.get(key)
        if lbl is None:
            return
        if text is not None:
            lbl.base_text = text
        base = getattr(lbl, "base_text", "")
        n = self.claim_counts.get(key, 0)
        lbl.configure(text=f"{base}  🎁 {n}" if n else base)

    def update_campaign_list(self, campaigns: list):
        selected_keys = {k for k, cb in self.campaign_checkboxes.items() if cb.get() == 1}
        for widget in self.campaigns_frame.winfo_children():
            widget.destroy()
        self.campaign_checkboxes.clear()
        self.campaign_progress.clear()
        self.campaign_percent.clear()
        self.campaign_status.clear()

        self.campaign_data = campaigns
        if not campaigns:
            ctk.CTkLabel(self.campaigns_frame, text="Нет доступных кампаний", text_color="gray").pack(pady=20)
            return

        for c in campaigns:
            key = c["key"]
            frame = ctk.CTkFrame(self.campaigns_frame)
            frame.pack(fill="x", padx=10, pady=5)

            cb = ctk.CTkCheckBox(frame, text=f"{c['name']} ({c['game']})")
            cb.pack(side="left", padx=10, pady=5)
            if key in selected_keys:
                cb.select()

            status = ctk.CTkLabel(frame, text=c["section"], text_color="gray", width=110, anchor="e")
            status.pack(side="right", padx=5)
            status.base_text = c["section"]

            pl = ctk.CTkLabel(frame, text="0%", width=40)
            pl.pack(side="right", padx=5)
            pb = ctk.CTkProgressBar(frame, width=140)
            pb.pack(side="right", padx=5, pady=5)
            pb.set(0)

            self.campaign_checkboxes[key] = cb
            self.campaign_progress[key] = pb
            self.campaign_percent[key] = pl
            self.campaign_status[key] = status

        self.log(f"✅ Показано кампаний: {len(campaigns)}")

    # ---- shutdown -----------------------------------------------------------
    def _on_closing(self):
        if self._closing:
            return
        self._closing = True
        self.withdraw()  # disappear instantly, clean up in the background
        self.tray.stop()
        self.worker.shutdown(timeout=10)  # closes Chromium + Playwright (no orphan processes)
        self.destroy()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main():
    config = load_config()
    setup_logging(config)
    app = VKDropsMinerGUI(config)
    app.mainloop()


if __name__ == "__main__":
    main()
