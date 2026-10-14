"""Jittered autoclicker for Windows. Uses only the Python standard library (tkinter + ctypes).

Two modes:
    Anchor    - click repeatedly around one point.
    Sequence  - play the "Main" sequence in a loop. A step is a click at a recorded point
                (a shift-click if Shift was held), a color step (find that color on screen, move
                to it, click), a key step (a key, a combo like Ctrl+S, or a run of typed keys),
                a wait (a set time ± jitter, used instead of the normal delay between steps), or
                a "run" step that plays another named sequence (a sub-sequence). Any step can
                repeat a set number of times, so "run Loot ×5" plays the Loot sub-sequence five
                times. Sub-sequences can run other sub-sequences, but never themselves.
                Sequences, idle areas and settings can be saved to / loaded from a .json file.

Idle areas (own tab): spots the cursor wanders to when there's free time (delays between steps,
wait steps, optionally breaks). It picks one at random, travels there (first clicking the
area's click spot, if it has one), and slowly drifts around inside its hover radius. Idling is strictly lower
priority than the steps: it only starts if there's time to get there, hover a while, and still
make it back for the next step on schedule.

The cursor glides to each click point along a curved, eased path (with the occasional
overshoot-and-correct) instead of jumping there. Keys are held for a varying time, spaced by a
varying delay, and fast runs overlap slightly the way real typing does. Every timing, position
and movement has configurable jitter, drawn from bell-shaped distributions so most values sit
near the setting and extremes are rare.

Hotkeys (global, and not passed on to the focused app):
    F6   start / stop
    F7   start / stop recording (your clicks and key presses on other windows become steps)
    F8   set the anchor to the current mouse position (on the Idle areas tab: add an idle area there)
    F9   add a color step using the color under the cursor
    F10  pause / resume (resumes the step it paused on; the mouse is yours while paused)
"""
import ctypes
import json
import math
import os
import queue
import random
import re
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import filedialog, ttk

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
winmm = ctypes.windll.winmm

# Use physical pixel coordinates so the anchor matches the screen on scaled displays.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
SRCCOPY = 0x00CC0020
GA_ROOT = 2
WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
VK_LBUTTON = 0x01
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_F6 = 0x75
VK_F7 = 0x76
VK_F8 = 0x77
VK_F9 = 0x78
VK_F10 = 0x79
HOTKEYS = (VK_F6, VK_F7, VK_F8, VK_F9, VK_F10)
MAIN = "Main"        # the sequence that plays; every other sequence is a sub-sequence
MAX_DEPTH = 16       # deepest nesting of sub-sequences played

MODIFIERS = {0xA0: "Shift", 0xA1: "Shift", 0xA2: "Ctrl", 0xA3: "Ctrl",
             0xA4: "Alt", 0xA5: "Alt", 0x5B: "Win", 0x5C: "Win"}
# Keys whose scan code needs the "extended" flag (arrows, nav cluster, right-hand modifiers...).
EXTENDED_KEYS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E,
                 0x5B, 0x5C, 0x5D, 0x6F, 0x90, 0xA3, 0xA5}
KEY_NAMES = {0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x13: "Pause", 0x14: "CapsLock",
             0x1B: "Esc", 0x20: "Space", 0x21: "PgUp", 0x22: "PgDn", 0x23: "End", 0x24: "Home",
             0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down", 0x2C: "PrtSc",
             0x2D: "Insert", 0x2E: "Delete", 0x5D: "Menu", **MODIFIERS}
# Keys never recorded: generic Shift/Ctrl/Alt (the left/right versions are used instead) and
# this program's own hotkeys.
UNRECORDED_KEYS = {0x10, 0x11, 0x12, *HOTKEYS}
RECORDABLE_KEYS = [vk for vk in range(0x08, 0xFF) if vk not in UNRECORDED_KEYS]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


# Handle-taking/returning functions need pointer-sized types on 64-bit Python.
_H = ctypes.c_void_p
user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = _H
user32.GetForegroundWindow.restype = _H
user32.GetAncestor.argtypes = [_H, ctypes.c_uint]
user32.GetAncestor.restype = _H
user32.MonitorFromPoint.argtypes = [POINT, ctypes.c_uint]
user32.MonitorFromPoint.restype = _H
user32.GetMonitorInfoW.argtypes = [_H, ctypes.c_void_p]
user32.GetDC.argtypes = [_H]
user32.GetDC.restype = _H
user32.ReleaseDC.argtypes = [_H, _H]
user32.RegisterHotKey.argtypes = [_H, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), _H, ctypes.c_uint, ctypes.c_uint]
gdi32.CreateCompatibleDC.argtypes = [_H]
gdi32.CreateCompatibleDC.restype = _H
gdi32.CreateCompatibleBitmap.argtypes = [_H, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = _H
gdi32.SelectObject.argtypes = [_H, _H]
gdi32.SelectObject.restype = _H
gdi32.BitBlt.argtypes = [_H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         _H, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
gdi32.GetDIBits.argtypes = [_H, _H, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
                            ctypes.c_void_p, ctypes.c_uint]
gdi32.DeleteObject.argtypes = [_H]
gdi32.DeleteDC.argtypes = [_H]


def cursor_pos():
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def virtual_screen():
    """(left, top, width, height) of the area spanning all monitors."""
    return (user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))


def on_screen(x, y):
    return bool(user32.MonitorFromPoint(POINT(int(x), int(y)), 0))  # 0 = MONITOR_DEFAULTTONULL


def clamp_to_screen(x, y):
    """Nearest point to (x, y) that's on a monitor. Windows pins the cursor to the screen, so
    aiming past an edge would leave it somewhere other than where we think it is."""
    if on_screen(x, y):
        return x, y
    mon = user32.MonitorFromPoint(POINT(int(x), int(y)), 2)  # 2 = MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(mon, ctypes.byref(mi))
    r = mi.rcMonitor
    return min(max(x, r.left), r.right - 1), min(max(y, r.top), r.bottom - 1)


def send_mouse(flags, dx=0, dy=0):
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = MOUSEINPUT(dx, dy, 0, flags, 0, 0)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def send_key(vk, up=False):
    """Key event with both the virtual-key and scan code set, as a physical keyboard sends."""
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_KEYS else 0)
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(vk, user32.MapVirtualKeyW(vk, 0), flags, 0, 0)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def move_abs(x, y):
    """Real mouse-move event to pixel (x, y), in the 0..65535 coordinates SendInput expects."""
    left, top, w, h = virtual_screen()
    nx = ((x - left) * 65536 + w - 1) // w
    ny = ((y - top) * 65536 + h - 1) // h
    send_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny)


def grab(left, top, w, h):
    """Screenshot of a screen rectangle as raw BGRA bytes, row by row from the top."""
    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        gdi32.BitBlt(mem, 0, 0, w, h, screen, left, top, SRCCOPY)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32)  # negative = top-down
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)
        return buf.raw
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)


def pixel_color(x, y):
    b, g, r, _ = grab(x, y, 1, 1)
    return r, g, b


def search_region(x, y, r):
    """Square of radius r around (x, y), clipped to the screens; the whole desktop if r <= 0."""
    vl, vt, vw, vh = virtual_screen()
    if r <= 0:
        return vl, vt, vw, vh
    left, top = max(vl, x - r), max(vt, y - r)
    right, bottom = min(vl + vw, x + r + 1), min(vt + vh, y + r + 1)
    return left, top, max(1, right - left), max(1, bottom - top)


def find_color(rgb, tol, left, top, w, h):
    """All pixels in the rectangle within ±tol of rgb on every channel, as (x, y) screen coords."""
    buf = grab(left, top, w, h)
    mask = -1
    for channel, value in zip((0, 1, 2), reversed(rgb)):  # buffer is B, G, R, A
        table = bytes(1 if abs(v - value) <= tol else 0 for v in range(256))
        mask &= int.from_bytes(buf[channel::4].translate(table), "little")
    if mask <= 0:
        return []
    hits = mask.to_bytes(w * h, "little")
    return [(left + i % w, top + i // w) for i in (m.start() for m in re.finditer(b"\x01", hits))]


def largest_blob(hits, cell=6):
    """Group matching pixels into connected patches and return the biggest one."""
    cells = {}
    for p in hits:
        cells.setdefault((p[0] // cell, p[1] // cell), []).append(p)
    seen, best = set(), []
    for start in cells:
        if start in seen:
            continue
        seen.add(start)
        stack, blob = [start], []
        while stack:
            cx, cy = stack.pop()
            blob.extend(cells[(cx, cy)])
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (cx + dx, cy + dy)
                    if n in cells and n not in seen:
                        seen.add(n)
                        stack.append(n)
        if len(blob) > len(best):
            best = blob
    return best


def find_color_target(rgb, tol, region, radius):
    """Click point on the largest patch of the color: near its middle, jittered by up to
    `radius`, and always on a matching pixel. None if the color isn't on screen."""
    hits = find_color(rgb, tol, *region)
    if not hits:
        return None
    blob = largest_blob(hits)
    mx = sum(p[0] for p in blob) / len(blob)
    my = sum(p[1] for p in blob) / len(blob)
    center = min(blob, key=lambda p: (p[0] - mx) ** 2 + (p[1] - my) ** 2)
    pixels = set(blob)
    for _ in range(50):
        p = point_in_radius(*center, radius)
        if p in pixels:
            return p
    return center


def truncated_gauss(mean, sd, lo, hi):
    """Normal sample, resampled until it falls within [lo, hi]."""
    if sd <= 0:
        return min(hi, max(lo, mean))
    while True:
        v = random.gauss(mean, sd)
        if lo <= v <= hi:
            return v


def bell_jitter(base, jitter):
    """base ± jitter: bell-shaped around base, never outside the range."""
    return truncated_gauss(base, jitter / 2, base - jitter, base + jitter)


def human_delay(base_ms, jitter_ms, minimum_ms=1):
    """base ± jitter ms, returned in seconds."""
    return max(minimum_ms, bell_jitter(base_ms, jitter_ms)) / 1000.0


def point_in_radius(cx, cy, r):
    """Random integer point within a circle of radius r around (cx, cy). Never outside.

    Clustered toward the centre (like aiming at a spot) rather than spread evenly to the edge.
    """
    if r <= 0:
        return cx, cy
    while True:
        dx = round(random.gauss(0, r / 2))
        dy = round(random.gauss(0, r / 2))
        if dx * dx + dy * dy <= r * r:
            return cx + dx, cy + dy


def movement_time(dist, speed_pct, speed_jitter_pct):
    """Seconds to move `dist` px. Fitts's-law shaped: longer moves take longer, but not linearly."""
    if dist < 1:
        return 0.0
    t = 0.05 + 0.11 * math.log2(dist / 14 + 1)
    t *= bell_jitter(1.0, min(speed_jitter_pct, 90) / 100)
    return t * 100 / max(10, speed_pct)


def max_movement_time(dist, speed_pct, speed_jitter_pct):
    """Upper bound on how long movement_time() can make a move of `dist` px."""
    if dist < 1:
        return 0.0
    return ((0.05 + 0.11 * math.log2(dist / 14 + 1)) * (1 + min(speed_jitter_pct, 90) / 100)
            * 100 / max(10, speed_pct)) + 0.02


def point_in_circle(cx, cy, r):
    """Uniformly random integer point within radius r of (cx, cy)."""
    if r <= 0:
        return cx, cy
    while True:
        dx, dy = random.randint(-r, r), random.randint(-r, r)
        if dx * dx + dy * dy <= r * r:
            return cx + dx, cy + dy


def point_in_zone(cx, cy, r, near=None, hop=None):
    """Random on-screen point within radius r of (cx, cy), preferring one within `hop` px of
    `near`. Parts of a zone hanging off the screen edge are never picked."""
    fallback = None
    for _ in range(40):
        p = point_in_circle(cx, cy, r)
        if not on_screen(*p):
            continue
        if near is None or math.hypot(p[0] - near[0], p[1] - near[1]) <= hop:
            return p
        fallback = fallback or p
    return fallback or clamp_to_screen(cx, cy)


def curved_path(x0, y0, x1, y1, curve_pct):
    """Cubic Bezier from start to end that bows to one side by up to ±curve_pct of the distance.

    Returns (p, normal): p(s) gives the point at progress s in [0, 1]; normal is the unit
    vector perpendicular to the straight line.
    """
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy)
    nx, ny = -dy / dist, dx / dist
    bend = bell_jitter(0.0, curve_pct / 100) * min(dist, 600)
    a1, a2 = random.uniform(0.2, 0.4), random.uniform(0.6, 0.8)
    b1, b2 = bend * random.uniform(0.6, 1.2), bend * random.uniform(0.6, 1.2)
    c1x, c1y = x0 + dx * a1 + nx * b1, y0 + dy * a1 + ny * b1
    c2x, c2y = x0 + dx * a2 + nx * b2, y0 + dy * a2 + ny * b2

    def p(s):
        u = 1 - s
        return (u ** 3 * x0 + 3 * u * u * s * c1x + 3 * u * s * s * c2x + s ** 3 * x1,
                u ** 3 * y0 + 3 * u * u * s * c1y + 3 * u * s * s * c2y + s ** 3 * y1)

    return p, (nx, ny)


def key_timeline(combos, hold_ms, hold_jitter, delay_ms, delay_jitter):
    """Plan typing `combos` (each (modifier_vks, vk)) like a person.

    Returns time-sorted (seconds, vk, is_up) events. Each key is held for hold ± jitter; each key
    goes down delay ± jitter after the previous one, so when the delay is shorter than the hold,
    consecutive keys overlap the way fast typing does. Combos and lone modifiers never overlap
    with anything (that would change what gets typed): modifiers go down one after another,
    the key is pressed, then they're released in reverse.
    """
    events = []
    t = 0.0           # when the next key goes down
    barrier = 0.0     # nothing goes down before this (end of the last combo)
    released = {}     # vk -> when it was last released

    def hold():
        return human_delay(hold_ms, hold_jitter, 20)

    for mods, vk in combos:
        t = max(t, barrier, released.get(vk, -1.0) + 0.015)
        if mods or vk in MODIFIERS:
            t = max([t] + [at + random.uniform(0.02, 0.05) for at, _, up in events if up])
            for m in mods:
                events.append((t, m, False))
                t += random.uniform(0.025, 0.07)
            down, up = t, t + hold()
            events += [(down, vk, False), (up, vk, True)]
            for m in reversed(mods):
                up += random.uniform(0.015, 0.05)
                events.append((up, m, True))
            barrier = up + random.uniform(0.02, 0.05)
        else:
            down = t
            events += [(down, vk, False), (down + hold(), vk, True)]
        released[vk] = max(at for at, k, up in events if k == vk and up)
        t = down + human_delay(delay_ms, delay_jitter, 20)
    return sorted(events, key=lambda e: e[0])


def key_name(vk):
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    if vk in KEY_NAMES:
        return KEY_NAMES[vk]
    scan = user32.MapVirtualKeyW(vk, 0)
    buf = ctypes.create_unicode_buffer(32)
    if scan and user32.GetKeyNameTextW((scan << 16) | ((vk in EXTENDED_KEYS) << 24), buf, 32):
        return buf.value.title() if len(buf.value) > 1 else buf.value
    return f"VK{vk:02X}"


def combo_name(combo):
    mods, vk = combo
    return "+".join([MODIFIERS[m] for m in mods] + [key_name(vk)])


def keys_label(combos):
    """'key Ctrl+S' for one combo; 'type Hello<Enter>' for a run of keys."""
    if len(combos) == 1:
        return "key  " + combo_name(combos[0])
    out = []
    for mods, vk in combos:
        shift = bool(mods) and all(MODIFIERS[m] == "Shift" for m in mods)
        if 0x41 <= vk <= 0x5A and (shift or not mods):
            out.append(chr(vk) if shift else chr(vk).lower())
        elif not mods and (0x30 <= vk <= 0x39 or vk == 0x20):
            out.append(chr(vk))
        else:
            out.append(f"<{combo_name((mods, vk))}>")
    return "type  " + "".join(out)


def color_hex(rgb):
    return "#%02X%02X%02X" % rgb


def fmt_ms(ms):
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:g} s"


def parse_step(st):
    """Rebuild a step loaded from JSON (lists back to tuples), rejecting anything malformed."""
    kind = st["type"]
    if kind == "click":
        step = {"type": "click", "x": int(st["x"]), "y": int(st["y"])}
        if st.get("shift"):
            step["shift"] = True
    elif kind == "color":
        r, g, b = (int(c) for c in st["rgb"])
        step = {"type": "color", "x": int(st["x"]), "y": int(st["y"]), "rgb": (r, g, b)}
    elif kind == "keys":
        keys = tuple((tuple(int(m) for m in mods), int(vk)) for mods, vk in st["keys"])
        if not keys or any(m not in MODIFIERS for mods, _ in keys for m in mods):
            raise ValueError("bad key step")
        step = {"type": "keys", "keys": keys}
    elif kind == "wait":
        step = {"type": "wait", "ms": max(0, int(st["ms"])), "jitter": max(0, int(st["jitter"]))}
    elif kind == "call":
        step = {"type": "call", "seq": str(st["seq"])}
    else:
        raise ValueError(f"unknown step type {kind!r}")
    times = int(st.get("times", 1))
    if times < 1:
        raise ValueError("repeat count must be at least 1")
    if times > 1:
        step["times"] = times
    return step


def runs_into(sequences, start, target):
    """True if sequence `start` is `target` or runs it, directly or through other sub-sequences.
    Used to refuse 'run' steps that would make a sequence end up running itself."""
    seen, todo = set(), [start]
    while todo:
        name = todo.pop()
        if name == target:
            return True
        if name in seen:
            continue
        seen.add(name)
        todo += [st["seq"] for st in sequences.get(name, ()) if st["type"] == "call"]
    return False


def parse_idle_area(a):
    """Idle area from JSON. "click" is the [x, y] clicked before idling, or null; files from
    before click spots were separate stored true, meaning click the idle spot itself."""
    x, y = int(a["x"]), int(a["y"])
    click = a.get("click")
    if click is True:
        click = (x, y)
    elif click:
        cx, cy = (int(v) for v in click)
        click = (cx, cy)
    else:
        click = None
    return {"x": x, "y": y, "radius": max(0, int(a["radius"])), "click": click}


class Hotkeys(threading.Thread):
    """Global hotkeys via RegisterHotKey, which also keeps the key press from reaching the
    focused app (F6 would otherwise jump to a browser's address bar, F10 would open menus).
    Keys another program has already claimed are listed in `failed`."""

    def __init__(self, vks):
        super().__init__(daemon=True)
        self.vks = vks
        self.pressed = queue.SimpleQueue()
        self.failed = []
        self._ready = threading.Event()
        self.start()
        self._ready.wait(2)

    def run(self):
        for vk in self.vks:
            if not user32.RegisterHotKey(None, vk, MOD_NOREPEAT, vk):
                self.failed.append(vk)
        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.pressed.put(msg.wParam)


class _Paused(Exception):
    """Raised mid-step when the user pauses; the step is retried from its start on resume."""


class Clicker:
    def __init__(self):
        self.settings = {}           # snapshot written by the UI thread, read by the worker
        self.status = "Stopped"
        self.clicks = 0
        self.keys = 0
        self.paused = False
        self.stack = ()              # ((sequence, step index), ...) from Main down to the step playing
        self.current_idle = None     # index of the idle area being hovered
        self.last_pos = (0, 0)       # where we last left the cursor
        self.step_pos = (0, 0)       # where the last step (not idling) left the cursor
        self.user_moved = False
        self._stop = threading.Event()
        self._thread = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self.user_moved = False
        self.paused = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        winmm.timeBeginPeriod(1)  # 1 ms sleep resolution for smooth movement
        try:
            self._loop()
        finally:
            winmm.timeEndPeriod(1)
            self.stack = ()
            self.paused = False
            if self.user_moved:
                self.status = "Stopped (you moved the mouse)"
            elif not self.status.startswith("Stopped"):
                self.status = "Stopped"

    def _loop(self):
        self.last_pos = self.step_pos = cursor_pos()
        next_action = time.perf_counter()
        # Play position as a stack of [sequence, step index, repetitions done], Main at the
        # bottom. A "run" step pushes the sub-sequence; finishing it pops back and counts one
        # repetition of that run step.
        stack = []
        played = False  # whether this pass through Main did anything
        while not self._stop.is_set():
            s = self.settings
            is_wait = False
            try:
                if s["mode"] != "sequence":
                    self.stack = ()
                    self.status = "Clicking"
                    done = self._move_and_click(point_in_radius(s["x"], s["y"], s["radius"]),
                                                next_action, why="next: click")
                else:
                    seqs = s["sequences"]
                    if not stack:
                        stack.append([MAIN, 0, 0])
                    frame = stack[-1]
                    steps = seqs.get(frame[0], ())
                    if frame[1] >= len(steps):  # reached the end of this sequence
                        stack.pop()
                        if stack:
                            stack[-1][2] += 1
                        elif not played:
                            self.status = "Stopped: nothing to play (the sequences have no steps)"
                            break
                        else:
                            played = False  # Main starts over
                        continue
                    st = steps[frame[1]]
                    if frame[2] >= st.get("times", 1):
                        frame[1] += 1
                        frame[2] = 0
                        continue
                    if st["type"] == "call":
                        if (st["seq"] not in seqs or len(stack) >= MAX_DEPTH
                                or any(f[0] == st["seq"] for f in stack)):
                            frame[2] = st.get("times", 1)  # missing or would run itself: skip
                        else:
                            stack.append([st["seq"], 0, 0])
                        continue
                    self.stack = tuple((name, idx) for name, idx, _ in stack)
                    is_wait = st["type"] == "wait"
                    done = self._run_step(st, self._path(stack, seqs), next_action)
                    played = True
            except _Paused:
                if self._hold_while_paused() is None:
                    break
                next_action = time.perf_counter()
                continue  # redo the same step
            if done is None:
                break
            if stack:
                stack[-1][2] += 1
            if done is False:
                continue  # step skipped
            if is_wait:
                next_action = done  # the wait stood in for the delay; no break chance either
                continue
            next_action = done + human_delay(s["interval"], s["interval_jitter"])

            if s["breaks"] and random.random() * 100 < s["break_chance"]:
                dur = human_delay(s["break_ms"], s["break_jitter"])
                self.status = f"On break ({dur:.1f}s)"
                end = time.perf_counter() + dur
                try:
                    if not (self._idle(end, "breaks", why="on break")
                            and self._wait(end - time.perf_counter())):
                        break
                except _Paused:
                    if self._hold_while_paused() is None:  # resuming ends the break
                        break
                next_action = time.perf_counter()

    @staticmethod
    def _path(stack, seqs):
        """Where we are, for the status line: 'Main 2 › Loot 3 [2/5]' (step numbers from 1,
        [repetition/total] for steps that repeat)."""
        parts = []
        for name, idx, rep in stack:
            steps = seqs.get(name, ())
            times = steps[idx].get("times", 1) if idx < len(steps) else 1
            parts.append(f"{name} {idx + 1}" + (f" [{rep + 1}/{times}]" if times > 1 else ""))
        return " › ".join(parts)

    def _run_step(self, st, label, next_action):
        """Run one sequence step. Returns when it finished, False if skipped, None if stopped."""
        s = self.settings
        if st["type"] == "wait":
            return self._wait_step(st, label)
        if st["type"] == "keys":
            self.status = f"Typing ({label})"
            # Keys may depend on what's hovered, so after idling (here or in a wait step before
            # this), go back to where the last click step left the cursor.
            home = self.step_pos
            if not self._idle(next_action, "delays", home, f"next: {label}"):
                return None
            if cursor_pos() != home:
                x0, y0 = cursor_pos()
                if not self._move_to(*home, self._move_time(math.hypot(home[0] - x0, home[1] - y0))):
                    return None
            if not (self._wait(next_action - time.perf_counter()) and self._type(st["keys"])):
                return None
            return time.perf_counter()
        if st["type"] == "color":
            # Search right before we'd start moving, so the position is fresh.
            x0, y0 = cursor_pos()
            lead = self._move_time(math.hypot(st["x"] - x0, st["y"] - y0)) + 0.08
            if not self._idle(next_action - lead, "delays", (st["x"], st["y"]), f"next: {label}"):
                return None
            if not self._wait(next_action - lead - time.perf_counter()):
                return None
            self.status = f"Looking for {color_hex(st['rgb'])} ({label})"
            target = self._find_color(st, s)
            if target is None:
                return None if self._stop.is_set() else False
            self.status = f"Clicking ({label})"
            return self._move_and_click(target, next_action, why=f"next: {label}")
        shift = st.get("shift", False)
        self.status = f"{'Shift-clicking' if shift else 'Clicking'} ({label})"
        return self._move_and_click(point_in_radius(st["x"], st["y"], s["radius"]), next_action, shift,
                                    why=f"next: {label}")

    def _wait_step(self, st, label):
        """Wait for the step's time ± jitter (hovering at an idle area if enabled), counting
        down in the status. A pause freezes the countdown; resuming continues with the time
        that was left. Returns the end time, or None if stopped."""
        end = time.perf_counter() + human_delay(st["ms"], st["jitter"], 0)
        try_idle = True
        while True:
            left = end - time.perf_counter()
            if left <= 0:
                return time.perf_counter()
            try:
                if try_idle:
                    try_idle = False
                    if not self._idle(end, "waits", why=label):
                        return None
                    continue
                self.status = f"Waiting {left:.1f}s ({label})"
                if not self._wait(min(left, 0.1)):
                    return None
            except _Paused:
                paused_for = self._hold_while_paused()
                if paused_for is None:
                    return None
                end += paused_for
                try_idle = True

    def _hold_while_paused(self):
        """Block while paused. Returns the seconds spent paused, or None if stopped meanwhile."""
        start = time.perf_counter()
        status, self.status = self.status, "Paused (F10 to resume)"
        while self.paused and not self._stop.wait(0.03):
            pass
        if self._stop.is_set():
            return None
        self.status = status
        self.last_pos = cursor_pos()  # the mouse was yours while paused; don't count that as a move
        return time.perf_counter() - start

    def _move_and_click(self, target, press_at, shift=False, why=""):
        """Glide to target and click (holding Shift if `shift`), timed so the press lands at
        press_at when possible. Free time beforehand may be spent at an idle area.
        Returns the press time, or None if stopped."""
        tx, ty = target = clamp_to_screen(*target)
        settle = random.uniform(0.04, 0.12)  # brief pause on target before pressing
        lead = random.uniform(0.05, 0.15) if shift else 0.0  # Shift goes down this long before the press
        if not self._idle(press_at - settle - lead, "delays", target, why):
            return None
        x0, y0 = cursor_pos()
        move_t = self._move_time(math.hypot(tx - x0, ty - y0))

        # Clicking faster than a natural move allows: hurry the move to keep the interval.
        budget = press_at - time.perf_counter()
        if move_t + settle + lead > budget > 0:
            k = budget / (move_t + settle + lead)
            move_t, settle, lead = move_t * k, settle * k, lead * k

        if not self._wait(press_at - move_t - settle - lead - time.perf_counter()):
            return None
        if not self._move_to(tx, ty, move_t):
            return None
        self.step_pos = self.last_pos
        if not self._wait(settle):
            return None
        shift_down = False
        try:
            if shift:
                send_key(VK_LSHIFT)
                shift_down = True
                if not self._wait(lead):
                    return None
            press = time.perf_counter()
            self._click()
            self.clicks += 1
            if shift:
                time.sleep(random.uniform(0.03, 0.10))  # let go of Shift a moment after the button
            return press
        finally:
            if shift_down:
                send_key(VK_LSHIFT, up=True)  # also on stop/pause, so Shift is never left held

    def _type(self, combos):
        """Play a key step. A pause takes effect at the next moment no key is held, and typing
        resumes from that key. False if stopped partway; any keys still down are released."""
        s = self.settings
        events = key_timeline(combos, s["key_hold"], s["key_hold_jitter"],
                              s["key_delay"], s["key_delay_jitter"])
        held = set()
        start = time.perf_counter()
        try:
            for at, vk, up in events:
                if not self._wait(start + at - time.perf_counter(), pausable=False):
                    return False
                if not up and not held and self.paused:
                    paused_for = self._hold_while_paused()
                    if paused_for is None:
                        return False
                    start += paused_for
                send_key(vk, up)
                if up:
                    held.discard(vk)
                else:
                    held.add(vk)
            self.keys += len(combos)
            return True
        finally:
            for vk in held:
                send_key(vk, up=True)  # never leave a key stuck down

    def _idle(self, until, kind, next_target=None, why=""):
        """Spend free time until `until` hovering around a random idle area.

        Low priority: it only goes if there's time to get there, hover for at least the minimum
        idle time, and still reach next_target (if given) by `until`, using a worst-case move
        time for the trip back. `kind` ("delays", "waits" or "breaks") is checked against the
        "idle during" settings. Returns False if stopped, True otherwise (including when it
        didn't idle); a pause raises _Paused like everywhere else.
        """
        s = self.settings
        areas = s.get("idle_areas")
        if not (areas and s["idle"] and s[f"idle_in_{kind}"]):
            return True
        i = random.randrange(len(areas))
        area = areas[i]
        ax, ay, r = area["x"], area["y"], area["radius"]
        entry = point_in_zone(ax, ay, r)

        def longest(a, b):
            return max_movement_time(math.hypot(b[0] - a[0], b[1] - a[1]), s["speed"], s["speed_jitter"])

        reserve = 0.0
        if next_target:  # worst case: heading back from the far edge of the hover zone
            reserve = max_movement_time(math.dist((ax, ay), next_target) + r,
                                        s["speed"], s["speed_jitter"]) + 0.05
        here = cursor_pos()
        if area["click"]:
            # Click at the area's click spot first, then go and idle around its idle spot.
            click_at = clamp_to_screen(*point_in_radius(*area["click"], s["radius"]))
            trip = longest(here, click_at) + 0.35 + longest(click_at, entry)
        else:
            click_at = None
            trip = longest(here, entry)
        stop_at = until - reserve
        if stop_at - time.perf_counter() < trip + s["idle_min"] / 1000:
            return True

        status = self.status
        self.current_idle = i
        self.status = f"Idling at area {i + 1}" + (f" · {why}" if why else "")
        try:
            if click_at:
                if not (self._move_to(*click_at, self._move_time(math.dist(here, click_at)))
                        and self._wait(random.uniform(0.05, 0.15))):
                    return False
                self._click()
                self.clicks += 1
                if not self._wait(random.uniform(0.1, 0.3)):  # a beat before moving off
                    return False
            here = cursor_pos()
            if not self._move_to(*entry, self._move_time(math.dist(here, entry))):
                return False
            while True:
                pause = human_delay(s["drift_pause"], s["drift_pause_jitter"], 50)
                if time.perf_counter() + pause >= stop_at:
                    return self._wait(stop_at - time.perf_counter())
                if not self._wait(pause):
                    return False
                # Drift a short hop to another on-screen spot inside the hover zone.
                cx, cy = cursor_pos()
                nx, ny = point_in_zone(ax, ay, r, near=(cx, cy), hop=max(4, r * 0.6))
                drift_t = movement_time(math.hypot(nx - cx, ny - cy),
                                        s["speed"] * s["drift_speed"] / 100, s["speed_jitter"])
                if time.perf_counter() + drift_t >= stop_at:
                    return self._wait(stop_at - time.perf_counter())
                if not self._glide(nx, ny, drift_t):
                    return False
        finally:
            self.current_idle = None
            self.status = status

    def _find_color(self, st, s):
        """Search for a color step's color, retrying until it appears or the timeout passes."""
        region = search_region(st["x"], st["y"], s["search_radius"])
        deadline = time.perf_counter() + s["color_timeout"] / 1000
        while True:
            target = find_color_target(st["rgb"], s["tolerance"], region, s["radius"])
            if target:
                return target
            if time.perf_counter() >= deadline:
                self.status = f"{color_hex(st['rgb'])} not found, skipped"
                return None
            if not self._wait(human_delay(250, 100)):
                return None

    def _move_time(self, dist):
        return movement_time(dist, self.settings["speed"], self.settings["speed_jitter"])

    def _click(self):
        send_mouse(MOUSEEVENTF_LEFTDOWN)
        time.sleep(truncated_gauss(0.085, 0.02, 0.045, 0.16))  # realistic button hold
        send_mouse(MOUSEEVENTF_LEFTUP)

    def _wait(self, seconds, pausable=True):
        """Sleep; returns False early if stopped or (optionally) the user moves the mouse.
        Raises _Paused if paused and `pausable`."""
        end = time.perf_counter() + max(0.0, seconds)
        while True:
            if self._stop.wait(max(0.0, min(end - time.perf_counter(), 0.03))):
                return False
            if pausable and self.paused:
                raise _Paused
            if self.settings.get("stop_on_move") and self._user_moved():
                return False
            if time.perf_counter() >= end:
                return True

    def _user_moved(self):
        cx, cy = cursor_pos()
        lx, ly = self.last_pos
        if abs(cx - lx) > 3 or abs(cy - ly) > 3:
            self.user_moved = True
            self._stop.set()
            return True
        return False

    def _move_to(self, tx, ty, duration):
        x0, y0 = cursor_pos()
        dist = math.hypot(tx - x0, ty - y0)
        if dist > 120 and duration >= 0.25 and random.random() < 0.25:
            # Overshoot slightly past the target, then make a small correction, all within the
            # planned duration so the click still lands on time.
            over = dist * random.uniform(0.02, 0.07)
            ox = round(tx + (tx - x0) / dist * over + random.gauss(0, 2))
            oy = round(ty + (ty - y0) / dist * over + random.gauss(0, 2))
            if on_screen(ox, oy):
                end = time.perf_counter() + duration
                if not self._glide(ox, oy, duration * random.uniform(0.7, 0.8)):
                    return False
                if not self._wait(min(random.uniform(0.03, 0.09), duration * 0.1)):
                    return False
                duration = max(0.04, end - time.perf_counter())
        return self._glide(tx, ty, duration)

    def _glide(self, tx, ty, duration):
        """Move from the current position to (tx, ty) over `duration` s. False if stopped."""
        tx, ty = clamp_to_screen(tx, ty)
        x0, y0 = cursor_pos()
        if math.hypot(tx - x0, ty - y0) >= 1 and duration > 0:
            path, (nx, ny) = curved_path(x0, y0, tx, ty, self.settings["curve"])
            skew = random.uniform(0.75, 0.95)  # reach peak speed early, longer slow-down
            last = (x0, y0)
            start = time.perf_counter()
            while True:
                if self._stop.is_set():
                    return False
                if self.paused:
                    raise _Paused
                t = (time.perf_counter() - start) / duration
                if t >= 1:
                    break
                tw = t ** skew
                s = tw ** 3 * (10 - 15 * tw + 6 * tw * tw)  # minimum-jerk easing
                x, y = path(s)
                wobble = random.gauss(0, 0.4) * math.sin(math.pi * s)  # hand tremor, none at ends
                pt = (round(x + nx * wobble), round(y + ny * wobble))
                if pt != last:
                    move_abs(*pt)
                    last = pt
                time.sleep(random.uniform(0.006, 0.010))  # ~100-165 Hz, like a real mouse
        move_abs(tx, ty)
        self.last_pos = (tx, ty)
        time.sleep(0.005)
        cx, cy = cursor_pos()
        if (cx, cy) != (tx, ty) and abs(cx - tx) <= 2 and abs(cy - ty) <= 2:
            user32.SetCursorPos(tx, ty)  # absolute coords can round 1px off; land exactly
        return True


class Tooltip:
    """Small help box shown when the mouse rests on a widget."""

    def __init__(self, widget, text, delay=500):
        self.widget, self.text, self.delay = widget, text, delay
        self._after = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event):
        self._hide()
        self._after = self.widget.after(self.delay, self._show)

    def _show(self):
        self._after = None
        tip = self._tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=self.text, justify="left", wraplength=300, background="#ffffe1",
                 relief="solid", borderwidth=1, padx=6, pady=4).pack()
        tip.update_idletasks()
        # Below the widget, kept on screen.
        left, top, w, h = virtual_screen()
        x = min(self.widget.winfo_rootx() + 10, left + w - tip.winfo_width() - 4)
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        if y + tip.winfo_height() > top + h:
            y = self.widget.winfo_rooty() - tip.winfo_height() - 4
        tip.wm_geometry(f"+{x}+{y}")

    def _hide(self, _event=None):
        if self._after:
            self.widget.after_cancel(self._after)
            self._after = None
        if self._tip:
            self._tip.destroy()
            self._tip = None


class App:
    TIPS = {
        "x": "Anchor point for 'Click around anchor' mode. Hover where you want it and press F8.",
        "y": "Anchor point for 'Click around anchor' mode. Hover where you want it and press F8.",
        "radius": "Clicks land within this many pixels of their target, mostly near the middle. "
                  "Also applies to color steps and idle-area clicks.",
        "interval": "Time from one step to the next (press to press, for clicks).",
        "interval_jitter": "Random ± change to each delay between steps. Most land near the "
                           "setting, never outside the range.",
        "key_hold": "How long each key is held down.",
        "key_hold_jitter": "Random ± change to each key's hold time.",
        "key_delay": "Time from one key press to the next inside a key step. Shorter than the "
                     "hold time means keys overlap, like fast typing.",
        "key_delay_jitter": "Random ± change to the time between keys.",
        "speed": "Overall mouse speed. 200 moves twice as fast, 50 half as fast.",
        "speed_jitter": "How much each move's speed varies, ± %.",
        "curve": "How far paths bow to one side, as ± % of the move's length. 0 = straight lines.",
        "tolerance": "How far each of red, green and blue may differ from the recorded color and "
                     "still count as a match.",
        "search_radius": "Only search this far around where the color was picked. 0 searches every screen.",
        "color_timeout": "If the color isn't on screen, keep looking this long, then skip the step.",
        "break_chance": "Chance of a break after each click or key step.",
        "break_ms": "How long a break lasts.",
        "break_jitter": "Random ± change to each break's length.",
        "idle_min": "Only go idle if the cursor can hover at least this long before it has to "
                    "head back for the next step.",
        "drift_speed": "Speed of the slow drifting moves while idling, as % of the normal move speed.",
        "drift_pause": "Time spent still between drifting moves.",
        "drift_pause_jitter": "Random ± change to each pause between drifts.",
    }
    GROUPS = [
        # title, grid row, grid column, fields: (key, label, default)
        ("Clicking", 1, 0, [
            ("x", "Anchor X", 500),
            ("y", "Anchor Y", 500),
            ("radius", "Click radius (px)", 5),
            ("interval", "Delay between steps (ms)", 1000),
            ("interval_jitter", "Delay jitter ± (ms)", 200),
        ]),
        ("Keys", 1, 1, [
            ("key_hold", "Key hold (ms)", 90),
            ("key_hold_jitter", "Key hold jitter ± (ms)", 35),
            ("key_delay", "Delay between keys (ms)", 140),
            ("key_delay_jitter", "Key delay jitter ± (ms)", 60),
        ]),
        ("Movement", 2, 0, [
            ("speed", "Move speed (%)", 100),
            ("speed_jitter", "Speed jitter ± (%)", 15),
            ("curve", "Path curve ± (%)", 20),
        ]),
        ("Color steps", 2, 1, [
            ("tolerance", "Tolerance ± (0-255)", 20),
            ("search_radius", "Search radius (px, 0 = all)", 0),
            ("color_timeout", "Wait timeout (ms)", 3000),
        ]),
        ("Breaks", 3, 0, [
            ("break_chance", "Chance per step (%)", 2),
            ("break_ms", "Break length (ms)", 10000),
            ("break_jitter", "Break jitter ± (ms)", 3000),
        ]),
    ]
    IDLE_FIELDS = [
        ("idle_min", "Min hover time (ms)", 1500),
        ("drift_speed", "Drift speed (%)", 35),
        ("drift_pause", "Pause between drifts (ms)", 900),
        ("drift_pause_jitter", "Drift pause jitter ± (ms)", 600),
    ]
    FIELDS = [f for group in GROUPS for f in group[3]] + IDLE_FIELDS
    CURRENT_BG = "#cfe3ff"

    def __init__(self, root):
        self.root = root
        self.clicker = Clicker()
        self.hotkeys = Hotkeys(HOTKEYS)
        self.vars = {}
        self.bool_vars = {}
        self.steps = []              # recorded sequence; see add_step() callers for the shapes
        self.idle_areas = []         # {"x", "y", "radius", "click"}
        self.recording = False
        self.note, self.note_until = "", 0.0   # temporary message for the status line
        self._shown_step = None      # step index currently highlighted in the list
        self._shown_idle = None      # idle area currently highlighted
        self._pending_click = None   # click spot captured by the first F8 of a two-press area
        self._keys_down = set()      # edge detection for polled keys
        self._rec_down = set()       # keys down at the last recording poll
        self._mod_unused = {}        # modifiers held with no other key pressed yet
        self._last_key_time = 0.0

        root.title("Jitter Clicker")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        frm = ttk.Frame(root, padding=10)
        frm.grid()
        self.notebook = ttk.Notebook(frm)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        seq_tab = ttk.Frame(self.notebook, padding=8)
        idle_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(seq_tab, text="Sequence")
        self.notebook.add(idle_tab, text="Idle areas")
        left = ttk.Frame(seq_tab)
        left.grid(row=0, column=0, sticky="n")
        right = ttk.LabelFrame(seq_tab, text="Recorded sequence", padding=6)
        right.grid(row=0, column=1, sticky="ns", padx=(10, 0))

        self.mode_var = tk.StringVar(value="anchor")
        modes = ttk.Frame(left)
        modes.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        w = ttk.Radiobutton(modes, text="Click around anchor", variable=self.mode_var, value="anchor")
        w.grid(row=0, column=0, padx=(0, 12))
        self._tip("Click over and over around the anchor point.", w)
        w = ttk.Radiobutton(modes, text="Repeat recorded sequence", variable=self.mode_var, value="sequence")
        w.grid(row=0, column=1)
        self._tip("Play the recorded steps on the right in a loop.", w)

        groups = {}
        for title, grow, gcol, fields in self.GROUPS:
            box = ttk.LabelFrame(left, text=title, padding=(8, 4))
            box.grid(row=grow, column=gcol, sticky="nsew", padx=3, pady=3)
            self._field_rows(box, fields)
            groups[title] = box

        w = ttk.Checkbutton(groups["Breaks"], text="Take random breaks", variable=self._bool("breaks", True))
        w.grid(row=len(self.GROUPS[-1][3]), column=0, columnspan=2, sticky="w", pady=(2, 0))
        self._tip("Now and then, stop for a while: no clicks, keys or mouse movement "
                  "(unless idling during breaks is on in the Idle areas tab).", w)

        controls = ttk.Frame(left, padding=(8, 4))
        controls.grid(row=3, column=1, sticky="nsew", padx=3, pady=3)
        controls.columnconfigure((0, 1), weight=1)
        w = ttk.Checkbutton(controls, text="Stop if I move the mouse", variable=self._bool("stop_on_move", True))
        w.grid(row=0, column=0, columnspan=2, sticky="w", pady=(4, 8))
        self._tip("Stop automatically if you take over the mouse (it's more than 3 px from where "
                  "it was left). Doesn't apply while paused.", w)
        self.toggle_btn = ttk.Button(controls, text="Start (F6)", command=self.toggle)
        self.toggle_btn.grid(row=1, column=0, sticky="ew", padx=(0, 3))
        self._tip("Start or stop (F6). Starting again begins from step 1.", self.toggle_btn)
        self.pause_btn = ttk.Button(controls, text="Pause (F10)", command=self.toggle_pause)
        self.pause_btn.grid(row=1, column=1, sticky="ew", padx=(3, 0))
        self._tip("Pause or resume (F10). Resumes the step it paused on; the mouse is yours "
                  "while paused.", self.pause_btn)
        w = ttk.Button(controls, text="Set anchor (F8)", command=self.set_anchor)
        w.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self._tip("Set the anchor to the mouse position. Hover where you want it and press F8.", w)

        self.listbox = tk.Listbox(right, height=16, width=32, activestyle="none",
                                  selectmode="extended")
        self.listbox.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self._tip("The steps, in order. ▶ marks the one in progress. Drag a step to move it. "
                  "Select steps to move, remove, or insert a wait after them (Shift/Ctrl-click "
                  "selects several).", self.listbox)
        self._drag_from = None
        self.listbox.bind("<ButtonPress-1>", self._drag_start, add="+")
        self.listbox.bind("<B1-Motion>", self._drag_motion)
        self.listbox.bind("<ButtonRelease-1>", self._drag_end, add="+")
        self.listbox.bind("<Control-Up>", lambda e: self.move_selected(-1) or "break")
        self.listbox.bind("<Control-Down>", lambda e: self.move_selected(1) or "break")
        self.record_btn = ttk.Button(right, text="Record (F7)", command=self.toggle_record)
        self.record_btn.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 2))
        self._tip("Start or stop recording (F7). Your clicks (and shift-clicks) and key presses on "
                  "other windows become steps. F9 adds a color step from the pixel under the cursor, "
                  "recording or not.", self.record_btn)
        wait_row = ttk.Frame(right)
        wait_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 2))
        wait_row.columnconfigure(4, weight=1)
        self.wait_ms_var = tk.StringVar(value="2000")
        self.wait_jitter_var = tk.StringVar(value="500")
        w = ttk.Entry(wait_row, textvariable=self.wait_ms_var, width=6)
        w.grid(row=0, column=0)
        self._tip("Length of the wait to add (ms).", w)
        ttk.Label(wait_row, text="±").grid(row=0, column=1, padx=2)
        w = ttk.Entry(wait_row, textvariable=self.wait_jitter_var, width=5)
        w.grid(row=0, column=2)
        self._tip("Random ± change to the wait each time it plays (ms).", w)
        ttk.Label(wait_row, text="ms").grid(row=0, column=3, padx=(2, 4))
        w = ttk.Button(wait_row, text="Add wait", command=self.add_wait)
        w.grid(row=0, column=4, sticky="ew")
        self._tip("Insert a wait after the selected step (or at the end). It replaces the normal "
                  "delay between steps there.", w)
        edit_row = ttk.Frame(right)
        edit_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 2))
        edit_row.columnconfigure((2, 3), weight=1)
        for col, text, command, tip in (
                (0, "▲", lambda: self.move_selected(-1), "Move the selected steps up (Ctrl+↑ in the list)."),
                (1, "▼", lambda: self.move_selected(1), "Move the selected steps down (Ctrl+↓ in the list)."),
                (2, "Remove", self.remove_selected, "Remove the selected steps."),
                (3, "Clear", self.clear_steps, "Remove all steps.")):
            w = ttk.Button(edit_row, text=text, command=command, width=3 if col < 2 else 7)
            w.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 2, 0 if col == 3 else 2))
            self._tip(tip, w)
        for col, text, command, tip in (
                (0, "Save…", self.save, "Save the steps, idle areas and all settings to a file."),
                (1, "Load…", self.load, "Load steps, idle areas and settings from a saved file.")):
            w = ttk.Button(right, text=text, command=command)
            w.grid(row=4, column=col, sticky="ew", padx=(0, 2) if col == 0 else (2, 0), pady=(0, 2))
            self._tip(tip, w)

        self._build_idle_tab(idle_tab)

        self.status_var = tk.StringVar()
        ttk.Label(frm, textvariable=self.status_var, foreground="#555", wraplength=680).grid(
            row=1, column=0, sticky="w", pady=(6, 0))  # wraps rather than widening the window

        if self.hotkeys.failed:
            taken = ", ".join(key_name(vk) for vk in self.hotkeys.failed)
            self.show_note(f"{taken} already claimed by another program; they'll also reach the focused app", 8)
        else:
            self.show_note("Tip: rest the mouse on any setting or button for an explanation", 6)
        self.tick()

    def _tip(self, text, *widgets):
        for w in widgets:
            Tooltip(w, text)

    def _bool(self, key, default):
        var = tk.BooleanVar(value=default)
        self.bool_vars[key] = var
        return var

    def _field_rows(self, box, fields):
        """Label + entry rows for numeric settings, with their tooltips."""
        box.columnconfigure(0, weight=1)
        for row, (key, label, default) in enumerate(fields):
            lbl = ttk.Label(box, text=label)
            lbl.grid(row=row, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=str(default))
            entry = ttk.Entry(box, textvariable=var, width=8)
            entry.grid(row=row, column=1, pady=2, padx=(6, 0))
            self.vars[key] = var
            if key in self.TIPS:
                self._tip(self.TIPS[key], lbl, entry)

    def _build_idle_tab(self, tab):
        left = ttk.Frame(tab)
        left.grid(row=0, column=0, sticky="n")
        right = ttk.LabelFrame(tab, text="Idle areas", padding=6)
        right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))

        w = ttk.Checkbutton(left, text="Idle at these areas when there's free time",
                            variable=self._bool("idle", True))
        w.grid(row=0, column=0, sticky="w", pady=(0, 6))
        self._tip("While waiting for the next step, wander to a random idle area and drift around "
                  "it. Idling never delays a step: it only starts when there's time to get there, "
                  "hover for the minimum time, and still be back on schedule.", w)
        when = ttk.LabelFrame(left, text="Idle during", padding=(8, 4))
        when.grid(row=1, column=0, sticky="ew", pady=3)
        for row, (key, label, default, tip) in enumerate((
                ("idle_in_delays", "Delays between steps", True, "The normal delay between steps."),
                ("idle_in_waits", "Wait steps", True, "Wait steps in the sequence."),
                ("idle_in_breaks", "Breaks", False, "Random breaks. Off by default, so breaks keep the mouse still."))):
            w = ttk.Checkbutton(when, text=label, variable=self._bool(key, default))
            w.grid(row=row, column=0, sticky="w")
            self._tip(tip, w)
        hover = ttk.LabelFrame(left, text="Hovering", padding=(8, 4))
        hover.grid(row=2, column=0, sticky="ew", pady=3)
        self._field_rows(hover, self.IDLE_FIELDS)

        self.idle_tree = ttk.Treeview(right, columns=("n", "spot", "r", "click"), show="headings",
                                      height=12, selectmode="extended")
        for col, title, width in (("n", "#", 32), ("spot", "Idle spot", 100),
                                  ("r", "Radius", 60), ("click", "Click spot", 100)):
            self.idle_tree.heading(col, text=title)
            self.idle_tree.column(col, width=width, anchor="center", stretch=False)
        self.idle_tree.tag_configure("current", background=self.CURRENT_BG)
        self.idle_tree.grid(row=0, column=0, columnspan=3, sticky="nsew")
        self.idle_tree.bind("<<TreeviewSelect>>", self._on_idle_select)
        self._tip("Idle areas. The highlighted one is being hovered right now. Areas with a click "
                  "spot get clicked there first, then idled around the idle spot. Select areas to "
                  "see or change their settings.", self.idle_tree)

        opts = ttk.Frame(right)
        opts.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 4))
        lbl = ttk.Label(opts, text="Hover radius (px)")
        lbl.grid(row=0, column=0)
        self.area_radius_var = tk.StringVar(value="40")
        entry = ttk.Entry(opts, textvariable=self.area_radius_var, width=6)
        entry.grid(row=0, column=1, padx=(4, 12))
        self._tip("How far from its idle spot the cursor drifts.", lbl, entry)
        self.area_click_var = tk.BooleanVar(value=False)
        self.area_click_var.trace_add("write", self._on_area_click_toggle)
        w = ttk.Checkbutton(opts, text="Click before idling", variable=self.area_click_var)
        w.grid(row=0, column=2)
        self._tip("Click somewhere first, then go and idle at the area. With this ticked, adding an "
                  "area takes two F8 presses: first over the spot to click, then over the spot to "
                  "idle around.", w)
        right.columnconfigure((0, 1, 2), weight=1)
        for col, text, command, tip in (
                (0, "Apply to selected", self.apply_to_areas,
                 "Give the selected areas the hover radius above. Unticking 'Click before idling' "
                 "removes their click spot."),
                (1, "Remove", self.remove_areas, "Remove the selected areas."),
                (2, "Clear", self.clear_areas, "Remove all idle areas.")):
            w = ttk.Button(right, text=text, command=command)
            w.grid(row=2, column=col, sticky="ew", padx=(0 if col == 0 else 2, 0 if col == 2 else 2))
            self._tip(tip, w)
        ttk.Label(right, text="F8 adds an area at the mouse (with this tab open).",
                  foreground="#777").grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def show_note(self, text, seconds=4):
        self.note, self.note_until = text, time.monotonic() + seconds

    def read_settings(self):
        s = {}
        for key, _, default in self.FIELDS:
            try:
                s[key] = max(0, int(float(self.vars[key].get())))
            except ValueError:
                s[key] = self.clicker.settings.get(key, default)  # keep last good value while typing
        for key, var in self.bool_vars.items():
            s[key] = var.get()
        s["mode"] = self.mode_var.get()
        s["steps"] = tuple(self.steps)
        s["idle_areas"] = tuple(self.idle_areas)
        return s

    def set_anchor(self):
        x, y = cursor_pos()
        self.vars["x"].set(str(x))
        self.vars["y"].set(str(y))

    def toggle(self):
        self.note_until = 0
        if self.clicker.running:
            self.clicker.stop()
            return
        self.recording = False
        if self.mode_var.get() == "sequence" and not self.steps:
            self.show_note("Record or load a sequence first")
            return
        self.clicker.settings = self.read_settings()
        self.clicker.start()

    def toggle_pause(self):
        if self.clicker.running:
            self.clicker.paused = not self.clicker.paused

    def toggle_record(self):
        self.note_until = 0
        if self.clicker.running:
            self.show_note("Stop clicking before recording")
            return
        self.recording = not self.recording
        if self.recording:
            self.mode_var.set("sequence")
            # Keys already held when recording starts aren't recorded.
            self._rec_down = {vk for vk in RECORDABLE_KEYS if user32.GetAsyncKeyState(vk) & 0x8000}
            self._mod_unused = {}
            self._last_key_time = 0.0

    def save(self):
        path = filedialog.asksaveasfilename(parent=self.root, title="Save pattern",
                                            defaultextension=".json",
                                            filetypes=[("Clicker pattern", "*.json"), ("All files", "*.*")])
        if not path:
            return
        settings = self.read_settings()
        del settings["steps"], settings["idle_areas"]
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "settings": settings, "steps": self.steps,
                           "idle_areas": self.idle_areas}, f, indent=1)
        except OSError as e:
            self.show_note(f"Couldn't save: {e.strerror or e}")
            return
        self.show_note(f"Saved {os.path.basename(path)}")

    def load(self):
        path = filedialog.askopenfilename(parent=self.root, title="Load pattern",
                                          filetypes=[("Clicker pattern", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            steps = [parse_step(st) for st in data.get("steps", [])]
            # Files saved before idle areas existed keep the current ones.
            areas = ([parse_idle_area(a) for a in data["idle_areas"]] if "idle_areas" in data
                     else self.idle_areas)
            settings = data.get("settings", {})
            for key, _, _ in self.FIELDS:
                if key in settings:
                    int(float(settings[key]))  # validate before changing anything
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            self.show_note(f"Couldn't load {os.path.basename(path)}: {e}", 6)
            return
        for key, var in self.vars.items():
            if key in settings:
                var.set(str(settings[key]))
        for key, var in self.bool_vars.items():
            if key in settings:
                var.set(bool(settings[key]))
        if settings.get("mode") in ("anchor", "sequence"):
            self.mode_var.set(settings["mode"])
        self.steps = steps
        self.idle_areas = areas
        self._refresh_list()
        self._refresh_idle_tree()
        self.show_note(f"Loaded {os.path.basename(path)} ({len(steps)} steps, {len(areas)} idle areas)")

    def add_idle_area(self):
        """F8 on the Idle tab. With 'Click before idling' ticked, the first press saves the click
        spot and the second sets the idle spot; otherwise one press adds a plain area."""
        opts = self._area_options()
        if not opts:
            return
        radius, click = opts
        x, y = cursor_pos()
        if click and self._pending_click is None:
            self._pending_click = (x, y)
            return
        self.idle_areas.append({"x": x, "y": y, "radius": radius,
                                "click": self._pending_click if click else None})
        self._pending_click = None
        self._refresh_idle_tree()
        self.idle_tree.see(str(len(self.idle_areas) - 1))

    def _on_area_click_toggle(self, *_):
        if not self.area_click_var.get():
            self._pending_click = None  # unticking cancels a half-made area

    def _area_options(self):
        try:
            return max(0, int(float(self.area_radius_var.get()))), self.area_click_var.get()
        except ValueError:
            self.show_note("Hover radius must be a number")
            return None

    def apply_to_areas(self):
        sel = [int(i) for i in self.idle_tree.selection()]
        if not sel:
            self.show_note("Select the areas to change first")
            return
        opts = self._area_options()
        if not opts:
            return
        radius, click = opts
        missing = 0
        for i in sel:
            area = self.idle_areas[i]
            missing += click and not area["click"]
            self.idle_areas[i] = {**area, "radius": radius, "click": area["click"] if click else None}
        self._refresh_idle_tree()
        self.idle_tree.selection_set([str(i) for i in sel])
        if missing:
            self.show_note(f"{missing} area(s) have no click spot. Re-create to add one: "
                           "F8 on the click spot, then on the idle spot.", 8)

    def remove_areas(self):
        for i in sorted((int(i) for i in self.idle_tree.selection()), reverse=True):
            del self.idle_areas[i]
        self._refresh_idle_tree()

    def clear_areas(self):
        self.idle_areas.clear()
        self._refresh_idle_tree()

    def _refresh_idle_tree(self):
        self.idle_tree.delete(*self.idle_tree.get_children())
        for i, a in enumerate(self.idle_areas):
            click = "%d, %d" % a["click"] if a["click"] else ""
            self.idle_tree.insert("", "end", iid=str(i), tags=("current",) if i == self._shown_idle else (),
                                  values=(i + 1, f"{a['x']}, {a['y']}", a["radius"], click))

    def _on_idle_select(self, _event):
        sel = self.idle_tree.selection()
        if sel:
            a = self.idle_areas[int(sel[0])]
            self.area_radius_var.set(str(a["radius"]))
            self.area_click_var.set(a["click"] is not None)

    def _update_idle_highlight(self):
        cur = self.clicker.current_idle if self.clicker.running else None
        if cur == self._shown_idle:
            return
        old, self._shown_idle = self._shown_idle, cur
        for i in (old, cur):
            if i is not None and i < len(self.idle_areas):
                self.idle_tree.item(str(i), tags=("current",) if i == cur else ())

    def add_step(self, step):
        self.steps.append(step)
        self.mode_var.set("sequence")
        self._refresh_list()
        self.listbox.see("end")

    def add_color_step(self):
        x, y = cursor_pos()
        self.add_step({"type": "color", "x": x, "y": y, "rgb": pixel_color(x, y)})

    def add_wait(self):
        """Insert a wait step after the selected step, or at the end if nothing is selected."""
        try:
            ms = max(0, int(float(self.wait_ms_var.get())))
            jitter = max(0, int(float(self.wait_jitter_var.get())))
        except ValueError:
            self.show_note("Wait time and jitter must be numbers")
            return
        sel = self.listbox.curselection()
        i = int(sel[-1]) + 1 if sel else len(self.steps)
        self.steps.insert(i, {"type": "wait", "ms": ms, "jitter": jitter})
        self.mode_var.set("sequence")
        self._refresh_list()
        self.listbox.selection_set(i)  # so repeated adds chain in order
        self.listbox.see(i)

    def add_key(self, combo):
        """Record a key press. Presses within 2 s of each other join one 'type' step."""
        now = time.perf_counter()
        last = self.steps[-1] if self.steps else None
        if last and last["type"] == "keys" and now - self._last_key_time < 2.0:
            self.steps[-1] = {"type": "keys", "keys": last["keys"] + (combo,)}
            self._render_item(len(self.steps) - 1)
            self.listbox.see("end")
        else:
            self.add_step({"type": "keys", "keys": (combo,)})
        self._last_key_time = now

    def remove_selected(self):
        for i in sorted(self.listbox.curselection(), reverse=True):
            del self.steps[int(i)]
        self._refresh_list()

    def clear_steps(self):
        self.steps.clear()
        self._refresh_list()

    def move_selected(self, delta):
        """Move the selected steps one place up (-1) or down (+1), keeping them selected."""
        sel = sorted(int(i) for i in self.listbox.curselection())
        if not sel:
            self.show_note("Select the steps to move first")
            return
        if sel[0] + delta < 0 or sel[-1] + delta >= len(self.steps):
            return  # already at the top / bottom
        for i in (sel if delta < 0 else reversed(sel)):
            self.steps[i], self.steps[i + delta] = self.steps[i + delta], self.steps[i]
        self._refresh_list()
        for i in sel:
            self.listbox.selection_set(i + delta)
        self.listbox.see(sel[0] + delta if delta < 0 else sel[-1] + delta)

    def _drag_start(self, event):
        # Plain press on a step starts a drag; Shift/Ctrl-clicks keep their selection meaning.
        i = self.listbox.nearest(event.y)
        plain = not event.state & 0x0005  # neither Shift nor Ctrl held
        self._drag_from = i if plain and 0 <= i < len(self.steps) else None

    def _drag_motion(self, event):
        if self._drag_from is None:
            return None  # let the listbox do its normal drag-select
        to = self.listbox.nearest(event.y)
        if 0 <= to < len(self.steps) and to != self._drag_from:
            self.steps.insert(to, self.steps.pop(self._drag_from))
            self._drag_from = to
            self._refresh_list()
            self.listbox.selection_set(to)
            self.listbox.see(to)
            self.listbox.config(cursor="sb_v_double_arrow")
        return "break"

    def _drag_end(self, _event):
        self._drag_from = None
        self.listbox.config(cursor="")

    def _refresh_list(self):
        self.listbox.delete(0, "end")
        for i in range(len(self.steps)):
            self._render_item(i, replace=False)

    def _render_item(self, i, replace=True):
        """(Re)draw list row i, marking it if it's the step in progress."""
        st = self.steps[i]
        current = i == self._shown_step
        selected = replace and self.listbox.selection_includes(i)
        if st["type"] == "color":
            text = f"find color {color_hex(st['rgb'])}"
        elif st["type"] == "keys":
            text = keys_label(st["keys"])
            text = text if len(text) <= 30 else text[:29] + "…"
        elif st["type"] == "wait":
            text = f"wait  {fmt_ms(st['ms'])}" + (f" ± {fmt_ms(st['jitter'])}" if st["jitter"] else "")
        else:
            text = f"{'shift-click' if st.get('shift') else 'click'}  ({st['x']}, {st['y']})"
        if replace:
            self.listbox.delete(i)
        self.listbox.insert(i, ("▶ " if current else "    ") + f"{i + 1}.  {text}")
        if st["type"] == "color":
            r, g, b = st["rgb"]
            light = 0.299 * r + 0.587 * g + 0.114 * b > 140
            self.listbox.itemconfig(i, background=color_hex(st["rgb"]),
                                    foreground="black" if light else "white")
        elif current:
            self.listbox.itemconfig(i, background=self.CURRENT_BG)
        if selected:
            self.listbox.selection_set(i)

    def _update_highlight(self):
        cur = self.clicker.current_step if self.clicker.running else None
        if cur == self._shown_step:
            return
        old, self._shown_step = self._shown_step, cur
        for i in (old, cur):
            if i is not None and i < len(self.steps):
                self._render_item(i)
        if cur is not None and cur < len(self.steps):
            self.listbox.see(cur)

    def _own_root(self):
        return user32.GetAncestor(self.root.winfo_id(), GA_ROOT)

    def _is_own_window(self, x, y):
        hwnd = user32.WindowFromPoint(POINT(x, y))
        return bool(hwnd) and user32.GetAncestor(hwnd, GA_ROOT) == self._own_root()

    def _poll_keys(self):
        """Record key presses made in other windows, with whatever modifiers were held."""
        down = {vk for vk in RECORDABLE_KEYS if user32.GetAsyncKeyState(vk) & 0x8000}
        pressed, released = down - self._rec_down, self._rec_down - down
        self._rec_down = down
        fg = user32.GetForegroundWindow()
        ours = bool(fg) and user32.GetAncestor(fg, GA_ROOT) == self._own_root()
        for vk in released:
            # A modifier tapped on its own (e.g. a lone Shift) is a key press in its own right.
            if self._mod_unused.pop(vk, False) and not ours:
                self.add_key(((), vk))
        for vk in sorted(pressed, key=lambda v: v not in MODIFIERS):  # modifiers first
            if vk in MODIFIERS:
                self._mod_unused[vk] = True
                continue
            mods = tuple(sorted(m for m in down if m in MODIFIERS))
            for m in mods:
                self._mod_unused[m] = False
            if not ours:
                self.add_key((mods, vk))

    def _pressed(self, vk):
        """True once per physical key press (edge-triggered), checked globally."""
        down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
        was = vk in self._keys_down
        if down:
            self._keys_down.add(vk)
        else:
            self._keys_down.discard(vk)
        return down and not was

    def tick(self):
        # Settings are re-read every tick, so edits apply live while running.
        self.clicker.settings = self.read_settings()
        clicked = self._pressed(VK_LBUTTON)
        if self.recording:
            self._poll_keys()
            if clicked:
                x, y = cursor_pos()
                shift = any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (VK_LSHIFT, VK_RSHIFT))
                for vk in (VK_LSHIFT, VK_RSHIFT):
                    if vk in self._mod_unused:
                        self._mod_unused[vk] = False  # Shift was used for this click, not a lone tap
                if not self._is_own_window(x, y):
                    self.add_step({"type": "click", "x": x, "y": y, **({"shift": True} if shift else {})})

        hits = set()
        while not self.hotkeys.pressed.empty():
            hits.add(self.hotkeys.pressed.get())
        for vk in self.hotkeys.failed:  # claimed by another program: fall back to polling
            if self._pressed(vk):
                hits.add(vk)
        if VK_F6 in hits:
            self.toggle()
        if VK_F7 in hits:
            self.toggle_record()
        if VK_F8 in hits:
            if self.notebook.index("current") == 1:
                self.add_idle_area()
            else:
                self.set_anchor()
        if VK_F9 in hits:
            self.add_color_step()
        if VK_F10 in hits:
            self.toggle_pause()

        running = self.clicker.running
        self.toggle_btn.config(text="Stop (F6)" if running else "Start (F6)")
        self.pause_btn.config(text="Resume (F10)" if self.clicker.paused else "Pause (F10)",
                              state="normal" if running else "disabled")
        self.record_btn.config(text="Stop recording (F7)" if self.recording else "Record (F7)")
        self._update_highlight()
        self._update_idle_highlight()
        if self.recording:
            status = f"Recording: click or type to add, F9 adds color, F7 to finish ({len(self.steps)} steps)"
        elif self._pending_click:
            status = "Click spot set at %d, %d. Now hover where it should idle and press F8." % self._pending_click
        elif time.monotonic() < self.note_until:
            status = self.note
        elif self.clicker.paused and not self.clicker.status.startswith("Paused"):
            status = "Pausing…"
        else:
            status = self.clicker.status
        self.status_var.set(f"{status}  |  clicks: {self.clicker.clicks}  keys: {self.clicker.keys}")
        self.root.after(15, self.tick)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
