"""Window discovery, resolution, DPI awareness, and state detection.

Foundation for capture.py. Everything here is screen-independent: a target is
identified by title/hwnd/pid and described by its placement, so capture never
depends on what is currently visible on screen (an occluded or off-screen window
resolves the same as a foreground one).
"""
from __future__ import annotations

import ctypes
import json
import os
import tempfile
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum

import pywintypes
import win32api
import win32con
import win32gui
import win32process


class WindowState(str, Enum):
    NORMAL = "normal"
    MINIMIZED = "minimized"
    MAXIMIZED = "maximized"


@dataclass(frozen=True)
class WindowInfo:
    """A resolved capture target. ``rect`` is physical pixels (PMv2)."""

    hwnd: int
    pid: int
    title: str
    class_name: str
    rect: tuple[int, int, int, int]  # (left, top, right, bottom)
    state: WindowState

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]


class TargetNotFound(Exception):
    """No live window matched the given title/hwnd/pid selector."""


class AmbiguousTarget(Exception):
    """A title/pid selector matched more than one window; the caller must narrow it.

    Carries ``matches`` (largest first) so the caller can list the candidates instead
    of silently grabbing one -- which could be a sensitive window the user never meant.
    """

    def __init__(self, matches: "list[WindowInfo]"):
        self.matches = matches
        super().__init__(f"{len(matches)} windows matched the selector")


_dpi_set = False


def enable_dpi_awareness() -> bool:
    """Opt into Per-Monitor-V2 DPI so PrintWindow yields physical (un-scaled) pixels.

    Without this, a window on a 150%-scaled monitor is captured at logical size and
    the bitmap is the wrong dimensions. Returns True if awareness is in effect.
    Idempotent: a second call (or awareness already set by the host) is a no-op.
    """
    global _dpi_set
    if _dpi_set:
        return True
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        # -4 == DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            _dpi_set = True
            return True
    except (AttributeError, OSError):
        pass
    try:
        # Fallback for builds without the context API; 2 == PER_MONITOR_DPI_AWARE.
        # shcore returns an HRESULT and windll does NOT raise on failure, so the
        # result must be inspected: S_OK means we set it, E_ACCESSDENIED means
        # awareness was already established. Any other HRESULT is a real failure --
        # do not claim success, or geometry reads proceed at the wrong DPI.
        hr = ctypes.windll.shcore.SetProcessDpiAwareness(2) & 0xFFFFFFFF
        if hr in (0x0, 0x80070005):  # S_OK, E_ACCESSDENIED (already set)
            _dpi_set = True
            return True
        return False
    except (AttributeError, OSError):
        return False


def window_state(hwnd: int) -> WindowState:
    """Classify a window as normal/minimized/maximized from its placement."""
    show = win32gui.GetWindowPlacement(hwnd)[1]
    if show == win32con.SW_SHOWMINIMIZED:
        return WindowState.MINIMIZED
    if show == win32con.SW_SHOWMAXIMIZED:
        return WindowState.MAXIMIZED
    return WindowState.NORMAL


def window_rect(hwnd: int, state: WindowState) -> tuple[int, int, int, int]:
    """Return the geometry to size a capture DC from.

    A minimized window reports an off-screen (-32000) GetWindowRect; its real size
    lives in the normal-position placement, so the DC must be sized from there or
    the capture comes back 0x0 / blank.
    """
    if state == WindowState.MINIMIZED:
        return win32gui.GetWindowPlacement(hwnd)[4]
    return win32gui.GetWindowRect(hwnd)


def window_info(hwnd: int) -> WindowInfo:
    """Snapshot a single window. Raises TargetNotFound if the handle is dead."""
    # Awareness must be set before the first geometry read, or rcNormalPosition and
    # GetWindowRect come back in logical (scaled) coords while the capture DC is
    # sized in physical pixels -- a mismatch that only shows on non-100% monitors.
    # window_info is the chokepoint every resolve path funnels through.
    enable_dpi_awareness()
    if not win32gui.IsWindow(hwnd):
        raise TargetNotFound(f"hwnd {hwnd} is not a live window")
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    state = window_state(hwnd)
    left, top, right, bottom = window_rect(hwnd, state)
    return WindowInfo(
        hwnd=hwnd,
        pid=pid,
        title=win32gui.GetWindowText(hwnd) or "",
        class_name=win32gui.GetClassName(hwnd) or "",
        rect=(int(left), int(top), int(right), int(bottom)),
        state=state,
    )


def list_windows(min_size: int = 16) -> list[WindowInfo]:
    """Visible top-level application windows, largest first.

    Drops the noise a user never means by "capture this app": untitled windows,
    sub-``min_size`` helper windows (e.g. mspaint's 1x1 'GDI+ Window'), and
    WS_EX_TOOLWINDOW owned popups. Minimized windows are kept and sized from their
    restored placement.
    """
    out: list[WindowInfo] = []

    def _collect(hwnd: int, _: object) -> bool:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if not win32gui.GetWindowText(hwnd):
                return True
            if win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW:
                return True
            info = window_info(hwnd)
        except (TargetNotFound, pywintypes.error):
            # A window can close between enumeration and inspection. Skip it -- never
            # let one dying window raise out of the callback and abort EnumWindows.
            return True
        if info.state != WindowState.MINIMIZED and (
            info.width < min_size or info.height < min_size
        ):
            return True
        out.append(info)
        return True

    win32gui.EnumWindows(_collect, None)
    out.sort(key=lambda w: w.width * w.height, reverse=True)
    return out


def find_matches(
    *, title: str | None = None, pid: int | None = None, exact: bool = False
) -> list[WindowInfo]:
    """Visible windows matching a title and/or pid filter, largest first."""
    windows = list_windows()
    if pid is not None:
        windows = [w for w in windows if w.pid == pid]
    if title:
        needle = title.lower()
        if exact:
            windows = [w for w in windows if w.title.lower() == needle]
        else:
            windows = [w for w in windows if needle in w.title.lower()]
    return windows


def resolve_target(
    *,
    title: str | None = None,
    hwnd: int | None = None,
    pid: int | None = None,
    exact: bool = False,
) -> WindowInfo:
    """Resolve a single capture target from a title/hwnd/pid selector.

    ``hwnd`` is authoritative. Otherwise the largest visible window matching the
    title/pid filter wins; callers that need to detect ambiguity use find_matches.
    Raises TargetNotFound when nothing matches, or ValueError if no selector is
    given (an empty selector would otherwise resolve to the largest window on the
    desktop -- a silent footgun for library callers).
    """
    if title is None and hwnd is None and pid is None:
        raise ValueError("resolve_target needs one of title/hwnd/pid")
    if hwnd is not None:
        return window_info(hwnd)
    matches = find_matches(title=title, pid=pid, exact=exact)
    if not matches:
        selector = title if title is not None else f"pid={pid}"
        raise TargetNotFound(f"no visible window matched {selector!r}")
    if len(matches) > 1:
        # Refuse to guess which of several matches is meant -- it could be a sensitive
        # window. The caller lists the candidates and narrows with hwnd/exact.
        raise AmbiguousTarget(matches)
    return matches[0]


def restore_background(hwnd: int, *, offscreen: bool = False) -> None:
    """Un-minimize a window, drop it to the back of z-order, optionally off-screen.

    SW_SHOWNOACTIVATE and HWND_BOTTOM are a best effort to keep the restore quiet,
    but Windows 11 still forces a minimized->restored window to the foreground; the
    foreground change cannot be prevented here (the caller re-minimizes after).

    With ``offscreen`` the same SetWindowPos that sends the window to the back also
    moves it past the right edge of the virtual desktop, so it is visible only for
    the single frame between the show and the move. SetWindowPos is used rather than
    SetWindowPlacement because the latter clamps rcNormalPosition back onto the
    desktop and so cannot push a window off-screen.
    """
    win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
    if offscreen:
        left = virtual_screen_bounds()[2] + 200
        win32gui.SetWindowPos(
            hwnd,
            win32con.HWND_BOTTOM,
            left,
            0,
            0,
            0,
            win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
        )
    else:
        win32gui.SetWindowPos(
            hwnd,
            win32con.HWND_BOTTOM,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
        )


def minimize(hwnd: int) -> None:
    """Return a window to the minimized state (used to undo restore_background)."""
    win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)


def virtual_screen_bounds() -> tuple[int, int, int, int]:
    """(left, top, right, bottom) spanning all monitors, in physical pixels."""
    x = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
    y = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
    cx = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
    cy = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
    return (x, y, x + cx, y + cy)


def restore_placement(hwnd: int, placement: tuple) -> None:
    """Reinstate a window placement (rcNormalPosition) captured before an off-screen
    move, so the user's next manual restore lands where the window used to be."""
    win32gui.SetWindowPlacement(hwnd, placement)


def _is_cloaked(hwnd: int) -> bool:
    """True if DWM has cloaked the window (e.g. a UWP app on another virtual desktop).

    Cloaked windows pass IsWindowVisible but are not actually drawn, so they must not
    count as obscuring another window.
    """
    cloaked = ctypes.c_int(0)
    DWMWA_CLOAKED = 14
    try:
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        )
    except (AttributeError, OSError):
        return False
    return hr == 0 and cloaked.value != 0


def _rects_overlap(a: tuple, b: tuple) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def rect_on_single_monitor(rect: tuple[int, int, int, int]) -> bool:
    """True if ``rect`` lies fully within one physical monitor.

    The virtual-screen bounding box can include gaps between mismatched monitors, so
    a window in a gap (or straddling two) would pass a bounds check yet not be fully
    backed by screen pixels -- a BitBlt there reads undefined desktop area. Requiring
    containment in a single monitor's rect avoids both the gap and the straddle.
    """
    monitor = win32api.MonitorFromRect(rect, win32con.MONITOR_DEFAULTTONULL)
    if not monitor:
        return False
    left, top, right, bottom = win32api.GetMonitorInfo(monitor)["Monitor"]
    return rect[0] >= left and rect[1] >= top and rect[2] <= right and rect[3] <= bottom


def is_unobscured(hwnd: int) -> bool:
    """True if no real (visible, non-cloaked) window above ``hwnd`` overlaps its rect.

    Used to decide whether a screen-read (BitBlt) of the window would return the
    window's own pixels rather than a covering window's. Errs toward False (treat
    ambiguous cases as obscured) so a screen-read is only attempted when safe.
    """
    rect = win32gui.GetWindowRect(hwnd)
    above = win32gui.GetWindow(hwnd, win32con.GW_HWNDPREV)
    while above:
        if (
            win32gui.IsWindowVisible(above)
            and not _is_cloaked(above)
            and _rects_overlap(rect, win32gui.GetWindowRect(above))
        ):
            return False
        above = win32gui.GetWindow(above, win32con.GW_HWNDPREV)
    return True


def _flush_frame(hwnd: int) -> None:
    """Force the shell to re-read a window's styles after an ex-style change.

    SetWindowLong alone does not notify the taskbar; without a SWP_FRAMECHANGED
    flush the WS_EX_TOOLWINDOW taskbar-button change (and its reversal) can be
    ignored until something else repaints the window.
    """
    win32gui.SetWindowPos(
        hwnd,
        0,
        0,
        0,
        0,
        0,
        win32con.SWP_NOMOVE
        | win32con.SWP_NOSIZE
        | win32con.SWP_NOZORDER
        | win32con.SWP_NOACTIVATE
        | win32con.SWP_FRAMECHANGED,
    )


def is_topmost(hwnd: int) -> bool:
    """True if the window has the always-on-top ex-style (WS_EX_TOPMOST)."""
    return bool(win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST)


def clear_topmost(hwnd: int) -> None:
    """Drop always-on-top so the window will honor HWND_BOTTOM (z-order, not style)."""
    win32gui.SetWindowPos(
        hwnd,
        win32con.HWND_NOTOPMOST,
        0,
        0,
        0,
        0,
        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
    )


def set_topmost(hwnd: int) -> None:
    """Reinstate always-on-top (undo clear_topmost)."""
    win32gui.SetWindowPos(
        hwnd,
        win32con.HWND_TOPMOST,
        0,
        0,
        0,
        0,
        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
    )


def apply_toolwindow(hwnd: int) -> int:
    """Temporarily hide the taskbar button by switching to a tool-window ex-style.

    WS_EX_TOOLWINDOW windows have no taskbar button, so the brief restore leaves no
    taskbar trace. Returns the original ex-style for restore_exstyle. Best-effort:
    some shells cache the button, so suppression is not guaranteed on every system.
    """
    ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    win32gui.SetWindowLong(
        hwnd, win32con.GWL_EXSTYLE, (ex | win32con.WS_EX_TOOLWINDOW) & ~win32con.WS_EX_APPWINDOW
    )
    _flush_frame(hwnd)
    return ex


def restore_exstyle(hwnd: int, exstyle: int) -> None:
    """Reinstate an ex-style saved by apply_toolwindow."""
    win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, exstyle)
    _flush_frame(hwnd)


# --- Orphan reconcile -----------------------------------------------------------
#
# If a capture process is killed (SIGKILL / taskkill / crash) during the brief window
# in which it has un-minimized a target for capture, the window is left stranded --
# worst case off-screen and invisible, with no way for the user to get it back. A
# write-ahead ledger records each parked window with enough state to restore it; the
# operator runs --reconcile to recover any window whose owning capture process is
# gone. atexit/the finally block handle clean exits; this covers the unclean ones.


def _ledger_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    folder = os.path.join(base, "capture")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "parked.json")


def _read_ledger() -> list:
    try:
        with open(_ledger_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


_write_seq = 0


def _write_ledger(entries: list) -> None:
    # Atomic publish: a uniquely-named temp + os.replace, so every reader sees a
    # complete file. No cross-process lock -- a lost update just means one strand
    # goes unrecorded (a missed recovery), acceptable for best-effort insurance.
    global _write_seq
    _write_seq += 1
    path = _ledger_path()
    tmp = f"{path}.{os.getpid()}.{_write_seq}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(entries, handle)
    os.replace(tmp, path)


def _process_creation_time(pid: int) -> str | None:
    """A process's creation time as a stable string, or None if it can't be read."""
    try:
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    except pywintypes.error:
        return None
    try:
        return str(win32process.GetProcessTimes(handle)["CreationTime"])
    except pywintypes.error:
        return None
    finally:
        win32api.CloseHandle(handle)


def _owner_is_dead(owner_pid: int | None, owner_creation: str | None) -> bool:
    """True only when the owning process is *positively* gone.

    Any ambiguity (access denied, unreadable, missing fields) returns False so a
    window a live capture process is still using is never touched -- the same
    err-toward-not-acting discipline as is_unobscured.
    """
    if not owner_pid:
        return False
    try:
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, owner_pid)
    except pywintypes.error as exc:
        # ERROR_INVALID_PARAMETER == no such process. Anything else (e.g. access
        # denied) is ambiguous -> treat as alive.
        return exc.winerror == 87
    try:
        creation = str(win32process.GetProcessTimes(handle)["CreationTime"])
    except pywintypes.error:
        return False
    finally:
        win32api.CloseHandle(handle)
    # PID reused by a different process instance -> the original owner is gone.
    return owner_creation is not None and creation != owner_creation


def _placement_to_list(placement: tuple) -> list:
    return [placement[0], placement[1], list(placement[2]), list(placement[3]), list(placement[4])]


def _list_to_placement(data: list) -> tuple:
    return (data[0], data[1], tuple(data[2]), tuple(data[3]), tuple(data[4]))


def _is_mine(entry: dict, me: int, my_creation: str | None) -> bool:
    # Match this process's own entries, including creation time so a reused capture
    # PID never deletes a *dead* prior owner's entry for the same hwnd.
    return (
        entry.get("owner_pid") == me
        and entry.get("owner_creation") == my_creation
    )


def park(hwnd: int, placement: tuple, was_topmost: bool, exstyle: int | None) -> None:
    """Record a window about to be restored for capture. Never raises -- a ledger
    failure must not fail the capture, which is the actual job."""
    try:
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        me = os.getpid()
        my_creation = _process_creation_time(me)
        entry = {
            "hwnd": hwnd,
            "pid": pid,
            "class": win32gui.GetClassName(hwnd) or "",
            "target_creation": _process_creation_time(pid),
            "placement": _placement_to_list(placement),
            "was_topmost": was_topmost,
            "exstyle": exstyle,
            "owner_pid": me,
            "owner_creation": my_creation,
        }
        entries = [
            e
            for e in _read_ledger()
            if not (isinstance(e, dict) and e.get("hwnd") == hwnd and _is_mine(e, me, my_creation))
        ]
        entries.append(entry)
        _write_ledger(entries)
    except Exception:
        pass


def unpark(hwnd: int) -> None:
    """Remove this process's ledger entry for a window after a clean re-minimize."""
    try:
        me = os.getpid()
        my_creation = _process_creation_time(me)
        entries = [
            e
            for e in _read_ledger()
            if not (isinstance(e, dict) and e.get("hwnd") == hwnd and _is_mine(e, me, my_creation))
        ]
        _write_ledger(entries)
    except Exception:
        pass


def _entry_matches_window(entry: dict) -> bool:
    hwnd = entry.get("hwnd")
    if not hwnd or not win32gui.IsWindow(hwnd):
        return False
    try:
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
    except pywintypes.error:
        return False
    if pid != entry.get("pid") or (win32gui.GetClassName(hwnd) or "") != entry.get("class"):
        return False
    # Best-effort: a recorded target creation time must still match, so the same
    # pid+class belonging to a different process instance isn't mistaken for ours.
    recorded = entry.get("target_creation")
    if recorded is not None:
        current = _process_creation_time(pid)
        if current is not None and current != recorded:
            return False
    return True


def _restore_stranded(entry: dict) -> None:
    hwnd = entry["hwnd"]
    if entry.get("exstyle") is not None:
        restore_exstyle(hwnd, entry["exstyle"])
    if entry.get("placement"):
        win32gui.SetWindowPlacement(hwnd, _list_to_placement(entry["placement"]))
    minimize(hwnd)
    if entry.get("was_topmost"):
        set_topmost(hwnd)


def count_stranded() -> int:
    """How many recoverable strands the ledger holds (dead owner, window still there).

    Runs on every normal capture for the discoverability notice, so a single
    malformed entry must never crash it -- each is guarded independently.
    """
    total = 0
    for entry in _read_ledger():
        try:
            if (
                isinstance(entry, dict)
                and _owner_is_dead(entry.get("owner_pid"), entry.get("owner_creation"))
                and _entry_matches_window(entry)
            ):
                total += 1
        except Exception:
            pass
    return total


def reconcile(restore: bool = True) -> list[str]:
    """Recover windows stranded by a crashed capture process.

    Acts only on entries whose owner is positively dead; entries for a live owner are
    left untouched (it may be mid-capture). Returns the titles of windows handled.
    A malformed or unprocessable entry is dropped rather than allowed to abort the
    pass (one corrupt entry must not brick the ledger), and a transient win32 failure
    keeps the entry for a later retry. Stale entries (window gone/reused) are dropped.
    """
    handled: list[str] = []
    keep: list = []
    for entry in _read_ledger():
        try:
            if not isinstance(entry, dict):
                continue
            if not _owner_is_dead(entry.get("owner_pid"), entry.get("owner_creation")):
                keep.append(entry)  # owner alive or unknown -> in use, do not touch
                continue
            if not _entry_matches_window(entry):
                continue  # window gone / hwnd reused -> drop stale entry
            if restore:
                try:
                    _restore_stranded(entry)
                except pywintypes.error:
                    keep.append(entry)  # transient win32 failure -> retry later
                    continue
            handled.append(win32gui.GetWindowText(entry["hwnd"]) or f"hwnd {entry['hwnd']}")
        except Exception:
            # Corrupt/unprocessable entry -> drop it so it can't brick future runs.
            pass
    _write_ledger(keep)
    return handled
