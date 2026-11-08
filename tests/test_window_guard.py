"""BrowserWindow with a fake Win32 layer: parking off-screen, restore, hook, guard thread."""
import contextlib
import time

import main as m

OFF = m.OFFSCREEN


class FakeUser32:
    def __init__(self, world):
        self.world = world

    def ShowWindow(self, h, cmd):
        if cmd == m.SW_HIDE:
            self.world[h]["vis"] = False
        elif cmd in (m.SW_SHOW, m.SW_RESTORE):
            self.world[h]["vis"] = True

    def SetWindowPos(self, h, after, x, y, w, hh, flags):
        self.world[h]["x"], self.world[h]["y"] = x, y
        if not (flags & m.SWP_NOSIZE):
            self.world[h]["w"], self.world[h]["h"] = w, hh

    def IsWindow(self, h): return h in self.world
    def IsWindowVisible(self, h): return self.world[h]["vis"]
    def IsIconic(self, h): return False
    def BringWindowToTop(self, h): pass
    def SetForegroundWindow(self, h): pass
    def GetSystemMetrics(self, i): return 1920 if i == 0 else 1080


@contextlib.contextmanager
def make():
    """BrowserWindow on a fake Win32 layer; module globals are restored afterwards."""
    world = {101: dict(x=300, y=200, w=1280, h=720, vis=True)}
    missing = object()
    saved = (m.IS_WINDOWS, getattr(m, "user32", missing))
    m.IS_WINDOWS, m.user32 = True, FakeUser32(world)
    w = m.BrowserWindow("tag", lambda msg: None)
    w._pids_cached = lambda force=False: {1}
    w._pids = {1}
    w._is_ours = lambda h, pids: h in world
    w._rect = lambda h: (world[h]["x"], world[h]["y"], world[h]["w"], world[h]["h"])
    w._top_windows = lambda pids: [(h, d["w"] * d["h"], d["vis"]) for h, d in world.items()]
    w._pump_messages = lambda ms: time.sleep(ms / 1000)
    w._install_hook = lambda: (None, None)
    w.GUARD_INTERVAL_S = 0.05
    try:
        yield w, world
    finally:
        w.stop_guard()
        m.IS_WINDOWS = saved[0]
        if saved[1] is missing:
            del m.user32
        else:
            m.user32 = saved[1]


def test_hide_parks_window_offscreen_and_show_restores_it():
    with make() as (w, world):
        assert w.hide()
        assert (world[101]["x"], world[101]["y"], world[101]["vis"]) == (OFF, OFF, False)
        assert w.is_hidden()
        w.show()
        assert (world[101]["x"], world[101]["y"], world[101]["w"], world[101]["h"]) == (300, 200, 1280, 720)
        assert world[101]["vis"] and not w.is_hidden()


def test_hook_conceals_new_window_instantly_and_ignores_other_objects():
    with make() as (w, world):
        w.hide()
        world[202] = dict(x=100, y=100, w=800, h=600, vis=True)
        w._on_win_event(None, m.EVENT_OBJECT_SHOW, 202, 0, 0, 0, 0)
        assert not world[202]["vis"] and world[202]["x"] == OFF
        world[303] = dict(x=100, y=100, w=800, h=600, vis=True)
        w._on_win_event(None, m.EVENT_OBJECT_SHOW, 303, -4, 0, 0, 0)  # not a window object
        assert world[303]["vis"]


def test_guard_thread_rehides_windows_shown_by_chromium():
    with make() as (w, world):
        w.want_hidden = True
        w.start_guard()
        try:
            world[101]["vis"] = True  # Chromium re-shows the window by itself
            time.sleep(0.3)
            assert not world[101]["vis"]
        finally:
            w.stop_guard()


def test_nothing_is_touched_while_the_user_wants_the_browser_visible():
    with make() as (w, world):
        w.want_hidden = False
        world[404] = dict(x=50, y=50, w=500, h=400, vis=True)
        w._on_win_event(None, m.EVENT_OBJECT_SHOW, 404, 0, 0, 0, 0)
        w._sweep()
        assert world[404]["vis"] and world[101]["vis"]
