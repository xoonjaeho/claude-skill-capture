"""Core window capture: render any window to a BGR ndarray, even when occluded.

Uses PrintWindow(PW_RENDERFULLCONTENT), which asks the window to paint itself into
an off-screen DC. Unlike a screen grab (BitBlt of the desktop), this reads the
window's own content, so a window hidden behind others still captures correctly.

Minimized windows are not composited by DWM and come back blank here; restoring
them in the background is layered on in a later step (see SKILL.md). This module
handles normal/maximized windows and exposes the primitives the rest builds on.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes

# Preflight: external deps are not auto-installed. Fail with guidance, not a
# raw traceback, so the operator knows to `pip install -r requirements.txt`.
try:
    import cv2
    import numpy as np
    import win32gui
    import win32ui
except ImportError as exc:  # pragma: no cover - environment guard
    sys.stderr.write(
        f"capture: missing dependency ({exc.name}). "
        "Install with: pip install -r requirements.txt\n"
    )
    raise SystemExit(2)

import win32con
import win32process

import windows
from windows import WindowInfo, WindowState, enable_dpi_awareness, resolve_target

PW_RENDERFULLCONTENT = 0x00000002

# Time for a freshly-restored window to paint before PrintWindow reads it. Tuned on
# this machine; too short yields a blank/partial first frame after restore.
_RESTORE_SETTLE_S = 0.2


class CaptureError(Exception):
    """Capture could not produce a frame for the target window."""


class MinimizedWindowError(CaptureError):
    """Target is minimized and restore_minimized was disabled by the caller."""


class OcrError(Exception):
    """OCR could not run (worker venv missing, worker failed, or bad output)."""


# OCR (PaddleOCR) needs Python <=3.13, so it runs in a dedicated subprocess venv. The
# path is config-fixed relative to this file -- never PATH-resolved, which on this box
# would find the 3.14 python that paddle can't install into.
# Generous because the FIRST OCR run downloads the PP-OCR models; later runs are fast.
_OCR_TIMEOUT_S = 300


def _ocr_python() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", ".venv-ocr", "Scripts", "python.exe"))


def _run_ocr(frame: "np.ndarray", lang: str) -> list:
    """Encode ``frame`` and hand it to the Python 3.13 PaddleOCR worker over a pipe.

    Returns a list of {text, confidence, box}. Raises OcrError if the OCR venv is not
    set up or the worker fails.
    """
    worker_python = _ocr_python()
    if not os.path.exists(worker_python):
        raise OcrError(
            f"OCR venv not found at {worker_python}. Set it up: "
            "py -3.13 -m venv .venv-ocr && "
            ".venv-ocr/Scripts/python -m pip install -r requirements-ocr.txt"
        )
    ok, png = cv2.imencode(".png", frame)
    if not ok:
        raise OcrError("failed to encode the frame for OCR")
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr_worker.py")
    try:
        proc = subprocess.run(
            [worker_python, worker, "--lang", lang],
            input=png.tobytes(),
            capture_output=True,
            timeout=_OCR_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise OcrError(f"OCR worker timed out after {_OCR_TIMEOUT_S}s (first run downloads models)")
    # Prefer the worker's structured result/error on stdout over its noisy stderr: a
    # decode failure or a missing-paddle venv reports a clean message there, while the
    # exit code alone would surface only paddle's glog chatter.
    try:
        data = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict) and "error" in data:
        raise OcrError(data["error"])
    if isinstance(data, dict) and "results" in data:
        return data["results"]
    # No parseable result -> the worker died before writing JSON (crash/OOM). Surface
    # its exit code and stderr.
    raise OcrError(
        f"OCR worker failed (exit {proc.returncode}): "
        f"{proc.stderr.decode('utf-8', 'replace').strip()[:500]}"
    )


def capture_text(
    target: "WindowInfo | int | None" = None,
    *,
    title: str | None = None,
    pid: int | None = None,
    region: tuple[int, int, int, int] | None = None,
    lang: str = "en",
) -> list:
    """Capture a window (or a region of it) and OCR it via the 3.13 PaddleOCR worker.

    Returns a list of {text, confidence, box}. The core stays on Python 3.14; only the
    OCR runs in the isolated 3.13 subprocess. Raises OcrError if OCR isn't set up.
    """
    frame = capture_window(target, title=title, pid=pid)
    if region is not None:
        frame = crop_region(frame, region)
    return _run_ocr(frame, lang)


def _capture_hwnd(hwnd: int, rect: tuple[int, int, int, int]) -> "np.ndarray":
    """PrintWindow ``hwnd`` into a fresh BGR ndarray sized from ``rect``.

    Returns shape (height, width, 3), dtype uint8, channel order BGR (OpenCV-ready;
    PrintWindow fills BGRA and the alpha column is dropped). Every GDI object is
    released in ``finally`` so a failing capture cannot leak DCs/bitmaps and starve
    the process-wide GDI handle pool.
    """
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise CaptureError(f"non-positive window size {width}x{height}")
    # A handle can die between resolve and capture; surface that as a clean
    # CaptureError instead of a raw GDI failure deep inside GetWindowDC.
    if not win32gui.IsWindow(hwnd):
        raise CaptureError(f"window {hwnd} is no longer alive")

    user32 = ctypes.windll.user32
    user32.PrintWindow.restype = wintypes.BOOL
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]

    # Acquire inside try so a failure midway (e.g. CreateCompatibleBitmap under GDI
    # pressure) still releases everything already acquired -- each release is guarded
    # against the object not having been created yet.
    window_dc = src_dc = mem_dc = bmp = prev = None
    try:
        window_dc = win32gui.GetWindowDC(hwnd)
        src_dc = win32ui.CreateDCFromHandle(window_dc)
        mem_dc = src_dc.CreateCompatibleDC()
        scratch = win32ui.CreateBitmap()
        scratch.CreateCompatibleBitmap(src_dc, width, height)
        bmp = scratch  # only now does a real HBITMAP exist to free
        prev = mem_dc.SelectObject(bmp)
        if not user32.PrintWindow(hwnd, mem_dc.GetSafeHdc(), PW_RENDERFULLCONTENT):
            # A false return means the window declined to render; don't hand back the
            # stale/empty DC contents as if they were a valid capture.
            raise CaptureError(f"PrintWindow failed for window {hwnd}")
        raw = bmp.GetBitmapBits(True)
        # Reshape assumes a 32-bit, tightly packed, top-down bitmap. That holds for a
        # compatible bitmap on a 32bpp desktop (universal on Win10/11); validate the
        # byte count so a violated assumption fails loud instead of corrupting pixels.
        # TODO: https://github.com/xoonjaeho/claude-skill-capture/issues/1 - switch to
        # a top-down 32bpp DIB section + GetDIBits in the Task 14 robustness pass.
        expected = width * height * 4
        if len(raw) != expected:
            raise CaptureError(
                f"unexpected bitmap layout: {len(raw)} bytes, expected {expected} "
                "(non-32bpp display?)"
            )
        frame = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 4)
        # .copy() detaches from the GDI buffer that finally is about to free.
        return frame[:, :, :3].copy()
    finally:
        # Deselect our bitmap before deleting it: DeleteObject refuses a bitmap
        # still selected into a DC, which would leak a GDI handle every frame.
        if prev is not None and mem_dc is not None:
            mem_dc.SelectObject(prev)
        if bmp is not None:
            win32gui.DeleteObject(bmp.GetHandle())
        if mem_dc is not None:
            mem_dc.DeleteDC()
        if src_dc is not None:
            src_dc.DeleteDC()
        if window_dc is not None:
            win32gui.ReleaseDC(hwnd, window_dc)


def _capture_minimized(
    info: WindowInfo, *, offscreen: bool = False, stealth: bool = False
) -> "np.ndarray":
    """Capture a minimized window by briefly restoring it, then re-minimizing it.

    Sequence: (optionally hide taskbar button / move off-screen) -> restore and send
    to the back of z-order -> let it paint -> PrintWindow -> re-minimize and undo
    every temporary change. The window is always re-minimized in ``finally`` so a
    capture failure cannot strand it un-minimized -- including the rare case where
    the user manually restores it during the settle, which this call will still pull
    back to minimized (an accepted side effect of the brief restore).

    FOCUS IS NOT PRESERVED: un-minimizing a window forces it to the foreground on
    Windows 11, and the foreground lock prevents handing focus back, so the target
    briefly (~_RESTORE_SETTLE_S) takes focus despite SW_SHOWNOACTIVATE. ``offscreen``
    reduces the *visible* flash to ~1 frame (SW_SHOWNOACTIVATE shows the window at
    its real position before the SetWindowPos that moves it off-screen) but the brief
    focus change remains. The focus-safe path is capturing a window that is merely
    occluded, not minimized.
    """
    hwnd = info.hwnd
    original_placement = None
    original_exstyle = None
    # Both restore paths send the window to HWND_BOTTOM, which strips WS_EX_TOPMOST,
    # so record it up front and reinstate it in finally regardless of offscreen/in-place.
    was_topmost = windows.is_topmost(hwnd)
    placement = win32gui.GetWindowPlacement(hwnd)  # pre-restore state, for ledger + restore
    # The ledger must record the ORIGINAL ex-style (pre-toolwindow) so reconcile can
    # undo a stealth change; read it now so park can run before apply_toolwindow.
    pre_exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) if stealth else None
    try:
        # Write-ahead BEFORE any change (including stealth) so a kill at any point in
        # the sequence leaves a recovery trail for --reconcile.
        windows.park(hwnd, placement, was_topmost, pre_exstyle)
        if stealth:
            original_exstyle = windows.apply_toolwindow(hwnd)
        if offscreen:
            # restore_background moves the window off-screen via SetWindowPos, which
            # also overwrites rcNormalPosition, so we restore the saved placement
            # afterward or a later user-restore lands off-screen.
            original_placement = placement
        elif was_topmost:
            # In-place: drop topmost before SW_SHOWNOACTIVATE so the window never even
            # briefly restores on top (offscreen mode moves it off-screen instead, so
            # z-order doesn't matter there).
            windows.clear_topmost(hwnd)
        windows.restore_background(hwnd, offscreen=offscreen)
        time.sleep(_RESTORE_SETTLE_S)
        # Re-read geometry after restore: the live rect is authoritative for sizing.
        restored = win32gui.GetWindowRect(hwnd)
        return _capture_hwnd(hwnd, restored)
    finally:
        # Undo every temporary change, each isolated: a failure in one step must not
        # skip the others (else an ex-style or off-screen placement is left stranded).
        for undo in (
            lambda: windows.minimize(hwnd),
            lambda: original_placement and windows.restore_placement(hwnd, original_placement),
            lambda: original_exstyle is not None and windows.restore_exstyle(hwnd, original_exstyle),
            lambda: was_topmost and windows.set_topmost(hwnd),
        ):
            try:
                undo()
            except Exception:
                pass
        # In-place mode has no placement backstop (offscreen mode re-minimizes as a
        # side effect of restoring the saved MINIMIZED placement). If the lone
        # minimize above failed, retry once rather than leave the window visible.
        if not offscreen and win32gui.IsWindow(hwnd):
            if windows.window_state(hwnd) != WindowState.MINIMIZED:
                try:
                    windows.minimize(hwnd)
                except Exception:
                    pass
        # Clean exit: drop the ledger entry so --reconcile won't act on this window.
        windows.unpark(hwnd)


def is_blank(frame: "np.ndarray") -> bool:
    """True if the frame is a single uniform color (PrintWindow rendered nothing).

    DWM does not composite some windows (minimized, exclusive-fullscreen, DRM), so
    PrintWindow returns a flat fill. A real UI is never perfectly uniform, which
    makes uniformity an unambiguous "we got nothing" signal.
    """
    return int(frame.max()) == int(frame.min())


def crop_region(frame: "np.ndarray", region: tuple[int, int, int, int]) -> "np.ndarray":
    """Crop ``frame`` to (x, y, w, h) in pixels of the captured image.

    Region coordinates are plain pixels within the returned capture (top-left
    origin) -- the capture is already physical pixels of the whole window, so this
    is exactly what the user sees in the saved image; no DPI/client-area translation
    is implied. Raises CaptureError if the region falls outside the frame.
    """
    x, y, w, h = region
    height, width = frame.shape[:2]
    if w <= 0 or h <= 0:
        raise CaptureError(f"region size must be positive, got {w}x{h}")
    if x < 0 or y < 0 or x + w > width or y + h > height:
        raise CaptureError(f"region {x},{y},{w},{h} is outside the {width}x{height} capture")
    return frame[y : y + h, x : x + w]


def blank_note(frame: "np.ndarray") -> str:
    """A short reason hint for a blank capture, or '' if the frame has content."""
    if not is_blank(frame):
        return ""
    return (
        "blank -- the window may be protected (DRM / exclusive-fullscreen / secure "
        "desktop), GPU-rendered in a way PrintWindow can't read, or genuinely solid"
    )


def _bitblt_capture(rect: tuple[int, int, int, int]) -> "np.ndarray":
    """Screen-read a screen rect via BitBlt -- whatever is *visible* there.

    Only correct when the window is unobscured (see _can_screen_grab); a covered
    window would return the covering pixels. Same guarded GDI lifecycle as
    _capture_hwnd, reading from the screen DC.
    """
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise CaptureError(f"non-positive window size {width}x{height}")
    screen_dc = src_dc = mem_dc = bmp = prev = None
    try:
        screen_dc = win32gui.GetDC(0)
        src_dc = win32ui.CreateDCFromHandle(screen_dc)
        mem_dc = src_dc.CreateCompatibleDC()
        scratch = win32ui.CreateBitmap()
        scratch.CreateCompatibleBitmap(src_dc, width, height)
        bmp = scratch
        prev = mem_dc.SelectObject(bmp)
        mem_dc.BitBlt((0, 0), (width, height), src_dc, (left, top), win32con.SRCCOPY)
        raw = bmp.GetBitmapBits(True)
        expected = width * height * 4
        if len(raw) != expected:
            raise CaptureError(f"unexpected bitmap layout: {len(raw)} bytes, expected {expected}")
        return np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 4)[:, :, :3].copy()
    finally:
        if prev is not None and mem_dc is not None:
            mem_dc.SelectObject(prev)
        if bmp is not None:
            win32gui.DeleteObject(bmp.GetHandle())
        if mem_dc is not None:
            mem_dc.DeleteDC()
        if src_dc is not None:
            src_dc.DeleteDC()
        if screen_dc is not None:
            win32gui.ReleaseDC(0, screen_dc)


def _can_screen_grab(hwnd: int) -> bool:
    """True if a BitBlt screen-read would return the window's own pixels: it sits
    fully within one monitor and is unobscured by any real window above it."""
    rect = win32gui.GetWindowRect(hwnd)
    return windows.rect_on_single_monitor(rect) and windows.is_unobscured(hwnd)


def _capture_with_fallback(hwnd: int, rect: tuple[int, int, int, int], fallback: bool) -> "np.ndarray":
    """PrintWindow the window; if blank and a screen-read would be valid, try BitBlt
    as an opportunistic upgrade. Never raises on blank -- a uniform frame is returned
    as-is (it may be a genuinely solid or protected window)."""
    frame = _capture_hwnd(hwnd, rect)
    if not (fallback and is_blank(frame) and _can_screen_grab(hwnd)):
        return frame
    grab_rect = win32gui.GetWindowRect(hwnd)
    try:
        alternate = _bitblt_capture(grab_rect)
    except CaptureError:
        return frame  # opportunistic upgrade failed; keep the original frame
    # Re-check the window did not move or get covered during the blit -- otherwise the
    # screen-read could include a covering window's pixels. On any drift, discard it.
    if (
        not is_blank(alternate)
        and win32gui.IsWindow(hwnd)
        and win32gui.GetWindowRect(hwnd) == grab_rect
        and _can_screen_grab(hwnd)
    ):
        return alternate
    return frame


def _revalidate(info: WindowInfo) -> None:
    """Raise CaptureError if the hwnd no longer identifies the same window.

    Windows recycles HWNDs, so between resolve and capture a handle can point at a
    different window. pid and class are stable for a window's lifetime; the title is
    not (editors mutate it with the open file / unsaved marker), so it is deliberately
    excluded from the check.
    """
    hwnd = info.hwnd
    if not win32gui.IsWindow(hwnd):
        raise CaptureError(f"window {hwnd} is no longer alive")
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    if pid != info.pid or win32gui.GetClassName(hwnd) != info.class_name:
        raise CaptureError(f"window {hwnd} was recycled; re-resolve the target")


def capture_window(
    target: WindowInfo | int | None = None,
    *,
    title: str | None = None,
    pid: int | None = None,
    restore_minimized: bool = False,
    offscreen: bool = True,
    stealth: bool = False,
    fallback: bool = True,
) -> "np.ndarray":
    """Capture a resolved window to a BGR ndarray.

    ``target`` may be a WindowInfo, an hwnd int, or omitted in favor of
    ``title``/``pid``. A window that is occluded (behind others) but not minimized
    captures correctly with zero side effects -- the focus-safe common case.

    A minimized target requires explicit opt-in via ``restore_minimized=True``,
    because un-minimizing it briefly steals foreground on Windows and that cannot be
    prevented (see _capture_minimized). When opted in, the restore is off-screen by
    default (``offscreen=True``, which cuts the visible flash to ~1 frame); pass
    ``offscreen=False`` to restore in place, and ``stealth=True`` to also hide the
    taskbar button. Without the opt-in, a minimized target raises MinimizedWindowError.

    With ``fallback`` (default), a non-minimized window that PrintWindow renders blank
    is retried with a screen-read (BitBlt) when that is provably safe (the window is
    on-screen and unobscured) -- this recovers some GPU windows. A still-blank result
    is returned as-is (never raised): a uniform frame may be a genuinely solid or
    protected window; use is_blank / blank_note to interpret it.
    """
    enable_dpi_awareness()
    info = _resolve(target, title, pid)
    _revalidate(info)

    if info.state == WindowState.MINIMIZED:
        if not restore_minimized:
            raise MinimizedWindowError(
                f"{info.title!r} is minimized. Pass restore_minimized=True to briefly "
                "un-minimize it for capture (a short focus change is unavoidable on "
                "Windows), or restore the window yourself -- capturing it behind other "
                "windows is fully focus-safe."
            )
        # Re-check live state: the user may have restored it since resolve. Only run
        # the restore/re-minimize dance if it is genuinely still minimized, so we
        # never minimize a window this call did not un-minimize.
        if windows.window_state(info.hwnd) == WindowState.MINIMIZED:
            return _capture_minimized(info, offscreen=offscreen, stealth=stealth)
        return _capture_with_fallback(info.hwnd, win32gui.GetWindowRect(info.hwnd), fallback)
    # Re-read the rect live: the window may have moved or resized since resolve.
    return _capture_with_fallback(info.hwnd, win32gui.GetWindowRect(info.hwnd), fallback)


def _resolve(
    target: WindowInfo | int | None, title: str | None, pid: int | None
) -> WindowInfo:
    """Resolve a target (WindowInfo / hwnd / title / pid) to a WindowInfo."""
    if isinstance(target, WindowInfo):
        return target
    if isinstance(target, int):
        return windows.window_info(target)
    return resolve_target(title=title, pid=pid)


def capture_stream(
    target: WindowInfo | int | None = None,
    *,
    title: str | None = None,
    pid: int | None = None,
    fps: float = 10.0,
    count: int | None = None,
    duration: float | None = None,
):
    """Yield BGR frames from a non-minimized window at approximately ``fps``.

    Focus-safe: the window is never moved or activated, only PrintWindow'd each
    tick, so streaming an occluded/background window does not disturb the user. A
    minimized target raises MinimizedWindowError -- streaming one would require
    holding it un-minimized (and thus holding focus) for the whole run, so restore
    it first. The window rect is re-read every frame, so a moved or resized window
    keeps capturing; iteration ends when the window closes, after ``count`` frames,
    or after ``duration`` seconds (whichever comes first; both None streams until the
    consumer stops).
    """
    if fps <= 0:
        raise ValueError("fps must be positive")
    enable_dpi_awareness()
    info = _resolve(target, title, pid)
    _revalidate(info)
    # Re-read live state (a passed-in WindowInfo may be stale) for consistency with
    # capture_window's TOCTOU guard.
    if windows.window_state(info.hwnd) == WindowState.MINIMIZED:
        raise MinimizedWindowError(
            f"{info.title!r} is minimized; restore it before streaming "
            "(streaming would hold focus for the whole run)"
        )
    hwnd = info.hwnd
    interval = 1.0 / fps
    start = time.monotonic()
    produced = 0
    while True:
        # Check the bounds before capturing so count=0 / duration=0 yield nothing.
        if count is not None and produced >= count:
            return
        if duration is not None and time.monotonic() - start >= duration:
            return
        tick = time.monotonic()
        if not win32gui.IsWindow(hwnd):
            return
        try:
            frame = _capture_hwnd(hwnd, win32gui.GetWindowRect(hwnd))
        except CaptureError:
            # The window can die between the IsWindow check and the capture; end the
            # stream cleanly instead of surfacing that race as an error.
            if not win32gui.IsWindow(hwnd):
                return
            raise
        yield frame
        produced += 1
        remaining = interval - (time.monotonic() - tick)
        if remaining > 0:
            time.sleep(remaining)


def wait_for_window(
    *,
    title: str | None = None,
    hwnd: int | None = None,
    pid: int | None = None,
    timeout: float = 30.0,
    interval: float = 0.5,
    exact: bool = False,
) -> WindowInfo:
    """Poll until a target window is resolvable, up to ``timeout`` seconds.

    For "launch an app, then capture its window once it appears". Raises
    TargetNotFound if the window never shows within the timeout.
    """
    enable_dpi_awareness()
    deadline = time.monotonic() + timeout
    while True:
        try:
            return resolve_target(title=title, hwnd=hwnd, pid=pid, exact=exact)
        except windows.TargetNotFound:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(interval, remaining))


def _slug(text: str) -> str:
    keep = [c if c.isalnum() else "_" for c in text.strip()]
    return ("".join(keep).strip("_") or "window")[:40]


def _ambiguous_message(matches: "list[WindowInfo]") -> str:
    # Sanitize titles: a window title with newlines/control chars could otherwise forge
    # extra candidate lines or corrupt the terminal.
    def oneline(text: str) -> str:
        return "".join(c if c.isprintable() else " " for c in text)[:55]

    shown = "\n".join(f"  --hwnd {m.hwnd:<9} {oneline(m.title)}" for m in matches[:10])
    more = "" if len(matches) <= 10 else f"\n  ... and {len(matches) - 10} more"
    return f"{len(matches)} windows match; disambiguate with --hwnd or --exact:\n{shown}{more}"


def _parse_region(text: str) -> tuple[int, int, int, int]:
    parts = text.split(",")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("region must be x,y,w,h")
    try:
        x, y, w, h = (int(p) for p in parts)
    except ValueError:
        raise argparse.ArgumentTypeError("region values must be integers")
    return x, y, w, h


def _print_list() -> int:
    enable_dpi_awareness()
    rows = windows.list_windows()
    print(f"{'HWND':>10}  {'PID':>7}  {'SIZE':>11}  {'STATE':<10}  TITLE")
    for w in rows:
        size = f"{w.width}x{w.height}"
        print(f"{w.hwnd:>10}  {w.pid:>7}  {size:>11}  {w.state.value:<10}  {w.title[:60]}")
    return 0


def _resolve_or_wait(args: argparse.Namespace) -> WindowInfo:
    """Resolve the target, polling up to --wait seconds if the window isn't up yet."""
    if args.wait is not None:
        return wait_for_window(
            title=args.title, hwnd=args.hwnd, pid=args.pid, timeout=args.wait, exact=args.exact
        )
    return resolve_target(title=args.title, hwnd=args.hwnd, pid=args.pid, exact=args.exact)


def _frames_due(elapsed: float, fps: float, written: int) -> int:
    """Number of times to write the current frame to keep the recorded mp4's playback
    clock aligned with wall-clock time.

    The output is constant-fps, but PrintWindow can't always sustain ``fps`` on a large
    window. Padding the lag with duplicate frames makes ``written`` track
    ``round(elapsed * fps)``, so the mp4 plays back in real time instead of fast. Always
    at least 1 so a captured frame is never dropped.
    """
    return max(1, round(elapsed * fps) - written)


def _run_stream(args: argparse.Namespace) -> int:
    enable_dpi_awareness()
    try:
        info = _resolve_or_wait(args)
    except windows.AmbiguousTarget as exc:
        sys.stderr.write(f"capture: {_ambiguous_message(exc.matches)}\n")
        return 1
    except windows.TargetNotFound as exc:
        sys.stderr.write(f"capture: {exc}\n")
        return 1
    # Bound the run: explicit count, else duration-only, else a 30-frame default so
    # `--loop FPS` alone benchmarks and exits rather than streaming forever.
    count = args.count if args.count is not None else (None if args.duration else 30)
    save = bool(args.out) and not args.no_save and not args.record
    stem, ext = "", ""
    if save:
        # splitext only splits the basename's extension, so a dotted directory in the
        # path (e.g. some.dir/shot) doesn't get mistaken for an extension.
        stem, ext = os.path.splitext(args.out)
        if not ext:
            ext = f".{args.format}"

    writer = None
    size = None
    rec_start = None
    written = 0
    frames = 0
    start = time.monotonic()
    try:
        for frame in capture_stream(info, fps=args.loop, count=count, duration=args.duration):
            if args.region is not None:
                frame = crop_region(frame, args.region)
            if args.record:
                if writer is None:
                    # mp4 encoders need even dimensions; rounding down (vs the encoder
                    # silently truncating an odd first frame) keeps every frame intact.
                    w0, h0 = frame.shape[1], frame.shape[0]
                    size = (w0 - w0 % 2, h0 - h0 % 2)
                    writer = cv2.VideoWriter(
                        args.record, cv2.VideoWriter.fourcc(*"mp4v"), args.loop, size
                    )
                    if not writer.isOpened():
                        sys.stderr.write(f"capture: cannot open {args.record} for writing\n")
                        return 1
                if rec_start is None:
                    rec_start = time.monotonic()  # anchor at the first written frame
                # VideoWriter needs a constant size; resize anything that differs.
                if (frame.shape[1], frame.shape[0]) != size:
                    frame = cv2.resize(frame, size)
                # Pad a slow capture up to real time: duplicate the current frame enough
                # to reach its wall-clock position, so the constant-fps mp4 plays back at
                # real speed rather than fast when PrintWindow lags behind --loop fps.
                reps = _frames_due(time.monotonic() - rec_start, args.loop, written)
                for _ in range(reps):
                    writer.write(frame)
                written += reps
            elif save:
                if not cv2.imwrite(f"{stem}_{frames:04d}{ext}", frame):
                    sys.stderr.write(f"capture: failed to write {stem}_{frames:04d}{ext}\n")
                    return 1
            frames += 1
    except MinimizedWindowError as exc:
        sys.stderr.write(f"capture: {exc}\n")
        return 3
    except (windows.TargetNotFound, CaptureError) as exc:
        sys.stderr.write(f"capture: {exc}\n")
        return 1
    finally:
        if writer is not None:
            writer.release()
    elapsed = time.monotonic() - start
    actual = frames / elapsed if elapsed > 0 else 0.0
    if frames and args.record:
        target = f", recorded {args.record}"
    elif frames and save:
        target = f", saved {stem}_NNNN{ext}"
    else:
        target = ""
    print(f"streamed {frames} frames in {elapsed:.2f}s = {actual:.1f} fps{target}")
    return 0


def _configure_stdio() -> None:
    """Make stdout/stderr tolerate any window title.

    Titles can hold characters the console codepage (e.g. cp949) cannot encode;
    without this a single foreign-language title crashes the whole listing.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(
        prog="capture", description="Capture a window (even when occluded) to PNG/ndarray."
    )
    parser.add_argument("--list", action="store_true", help="list visible windows and exit")
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="recover any window stranded by a crashed capture, then exit",
    )
    parser.add_argument("--title", help="select window by (partial) title")
    parser.add_argument("--hwnd", type=int, help="select window by handle")
    parser.add_argument("--pid", type=int, help="select window by process id")
    parser.add_argument(
        "--exact",
        action="store_true",
        help="match --title exactly (not as a substring)",
    )
    parser.add_argument(
        "--wait",
        type=float,
        nargs="?",
        const=30.0,
        metavar="S",
        help="poll up to S seconds (default 30) for the target window to appear",
    )
    parser.add_argument("--out", help="output image path (default: auto-named)")
    parser.add_argument(
        "--region",
        type=_parse_region,
        metavar="X,Y,W,H",
        help="crop to a region in pixels of the capture (top-left origin)",
    )
    parser.add_argument(
        "--format",
        choices=["png", "jpg"],
        default="png",
        help="output image format for the auto-named file (default: png)",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="don't write a file; just report the capture (size, blank?)",
    )
    parser.add_argument(
        "--ocr",
        action="store_true",
        help="OCR the capture and print the recognized text (needs the 3.13 OCR venv)",
    )
    parser.add_argument(
        "--lang",
        default="en",
        help="OCR language (default: en; e.g. korean, ch, japan)",
    )
    parser.add_argument(
        "--loop",
        type=float,
        metavar="FPS",
        help="stream a non-minimized window at FPS frames/sec (benchmarks fps)",
    )
    parser.add_argument("--count", type=int, help="with --loop, number of frames (default 30)")
    parser.add_argument("--duration", type=float, help="with --loop, seconds to stream")
    parser.add_argument(
        "--record",
        metavar="PATH",
        help="with --loop, record the stream to an mp4 (fps = --loop value)",
    )
    parser.add_argument(
        "--restore-minimized",
        action="store_true",
        help="opt in to briefly un-minimize a minimized target (short focus change on Windows)",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="with --restore-minimized, restore at the real position (brief flash) not off-screen",
    )
    parser.add_argument(
        "--stealth",
        action="store_true",
        help="with --restore-minimized, also hide the taskbar button during restore",
    )
    args = parser.parse_args(argv)

    if args.list:
        return _print_list()

    if args.reconcile:
        recovered = windows.reconcile(restore=True)
        if recovered:
            print(f"recovered {len(recovered)} stranded window(s): " + ", ".join(recovered))
        else:
            print("no stranded windows to recover")
        return 0

    if args.title is None and args.hwnd is None and args.pid is None:
        parser.error("specify --list, or one of --title/--hwnd/--pid")

    # Discoverability: an off-screen-stranded window is invisible, so the operator has
    # no way to know to run --reconcile. Surface it once, without acting automatically.
    stranded = windows.count_stranded()
    if stranded:
        sys.stderr.write(
            f"capture: {stranded} window(s) stranded by a prior crash; run --reconcile\n"
        )

    if args.record and args.loop is None:
        parser.error("--record requires --loop FPS (it records the stream)")
    if args.record and (args.out or args.no_save):
        parser.error("--record is exclusive with --out/--no-save")
    if args.loop is not None and args.loop <= 0:
        parser.error("--loop FPS must be positive")

    if args.loop is not None:
        return _run_stream(args)

    try:
        info = _resolve_or_wait(args)
        frame = capture_window(
            info,
            restore_minimized=args.restore_minimized,
            offscreen=not args.in_place,
            stealth=args.stealth,
        )
    except windows.AmbiguousTarget as exc:
        sys.stderr.write(f"capture: {_ambiguous_message(exc.matches)}\n")
        return 1
    except MinimizedWindowError as exc:
        sys.stderr.write(f"capture: {exc}\n")
        return 3
    except (windows.TargetNotFound, CaptureError) as exc:
        sys.stderr.write(f"capture: {exc}\n")
        return 1

    if args.region is not None:
        try:
            frame = crop_region(frame, args.region)
        except CaptureError as exc:
            sys.stderr.write(f"capture: {exc}\n")
            return 1

    if args.ocr:
        try:
            results = _run_ocr(frame, args.lang)
        except OcrError as exc:
            sys.stderr.write(f"capture: {exc}\n")
            return 1
        for item in results:
            print(item["text"])
        if not results:
            print("(no text recognized)")
        if not args.out:  # --ocr prints text; only also write a file if --out is given
            return 0

    height, width = frame.shape[:2]
    note = blank_note(frame)
    blank = f" ({note})" if note else ""
    if args.no_save:
        print(f"captured {width}x{height}{blank} (not saved)")
        return 0

    if args.out:
        # cv2.imwrite *raises* on an unknown/absent extension, so default one in.
        out = args.out if os.path.splitext(args.out)[1] else f"{args.out}.{args.format}"
    else:
        out = f"capture_{_slug(info.title)}_{int(time.time())}.{args.format}"
    if not cv2.imwrite(out, frame):
        sys.stderr.write(f"capture: failed to write {out}\n")
        return 1
    print(f"saved {out}  {width}x{height}{blank}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
