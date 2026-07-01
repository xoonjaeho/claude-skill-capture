"""Tests for core capture: blank detection, window resolution, and the
PrintWindow pixel pipeline against a controlled tkinter probe window.

The probe paints a known solid color into a uniquely-titled top-level window, so
an end-to-end capture can assert the real bytes that came back (R2: observable
result), not just that a call returned.
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path

import argparse
import os
import subprocess

import cv2
import numpy as np
import pytest
import win32api
import win32con
import win32gui
import win32ui

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import capture  # noqa: E402
import windows  # noqa: E402
from windows import WindowState  # noqa: E402

tk = pytest.importorskip("tkinter")

_shared_root = None


def _root():
    """One hidden Tk root for the whole session. Probes are Toplevels of it --
    creating a fresh tk.Tk() per test risks tkinter's multiple-interpreter state
    bugs (intermittent _tkinter.TclError under a full run)."""
    global _shared_root
    if _shared_root is None:
        _shared_root = tk.Tk()
        _shared_root.withdraw()
    return _shared_root


@contextmanager
def probe_window(color_hex: str, title: str, size: str = "320x240"):
    """A visible top-level Tk window of a known bg color, resolved to WindowInfo.

    Yields (window, WindowInfo). DPI awareness is enabled before the window exists so
    the captured bitmap is physical pixels and matches GetWindowRect.
    """
    windows.enable_dpi_awareness()
    win = tk.Toplevel(_root())
    win.title(title)
    win.geometry(size)
    win.configure(bg=color_hex)
    # Pump the event loop so the window actually paints before we capture it.
    for _ in range(20):
        win.update_idletasks()
        win.update()
        time.sleep(0.01)
    try:
        info = windows.resolve_target(title=title)
        yield win, info
    finally:
        win.destroy()


@pytest.mark.parametrize("exe", ["mspaint.exe", "notepad.exe"])
def test_capture_real_app_across_window_classes(exe):
    # The 4-app-class matrix in code form: a real classic-Win32 (mspaint, GDI) and a
    # real modern (notepad, WinUI) window must both PrintWindow to non-blank content.
    # The tkinter probes elsewhere are one window class; this guards the others.
    before = {w.hwnd for w in windows.list_windows()}
    proc = subprocess.Popen([exe])
    info = None
    try:
        for _ in range(60):
            time.sleep(0.1)
            fresh = [w for w in windows.list_windows() if w.hwnd not in before and w.width > 200]
            if fresh:
                info = max(fresh, key=lambda w: w.width * w.height)
                break
        if info is None:
            pytest.skip(f"{exe} did not open a window")
        frame = capture.capture_window(info)
        assert frame.shape[0] > 100 and frame.shape[1] > 100
        assert not capture.is_blank(frame)
    finally:
        # The real window may belong to a handed-off process (Win11 notepad), not the
        # launched stub, so terminate the window's own process by pid -- terminating
        # proc alone leaks the window.
        if info is not None:
            try:
                handle = win32api.OpenProcess(win32con.PROCESS_TERMINATE, False, info.pid)
                win32api.TerminateProcess(handle, 0)
                win32api.CloseHandle(handle)
            except Exception:
                pass
        proc.terminate()


def test_is_blank_true_for_uniform_frame():
    # A fully-failed PrintWindow comes back as one flat color; is_blank must flag it
    # so the fallback chain knows nothing was rendered.
    assert capture.is_blank(np.zeros((8, 8, 3), dtype=np.uint8)) is True


def test_is_blank_false_when_any_pixel_differs():
    # One differing pixel means real content was rendered; is_blank must not flag it.
    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    frame[3, 3] = (10, 20, 30)
    assert capture.is_blank(frame) is False


def test_resolve_by_title_returns_matching_window():
    title = "capture-probe-resolve-7f3a"
    with probe_window("#101010", title) as (_root, info):
        # Substring title resolution must land on the probe, not some other window.
        assert title in info.title
        assert info.hwnd != 0
        assert windows.window_state(info.hwnd) == WindowState.NORMAL


def test_capture_renders_known_color():
    # End-to-end: a blue probe must come back as a blue-dominant BGR frame of the
    # window's physical size. Catches a broken PrintWindow flag, wrong reshape, or
    # dropped/encoded channels (any of those flips the dominant channel or shape).
    title = "capture-probe-color-9b2c"
    with probe_window("#0000ff", title) as (_root, info):
        frame = capture.capture_window(info)

    assert frame.shape == (info.height, info.width, 3)
    assert frame.dtype == np.uint8
    assert not capture.is_blank(frame)

    h, w, _ = frame.shape
    center = frame[h // 3 : 2 * h // 3, w // 3 : 2 * w // 3]
    blue, green, red = center[..., 0].mean(), center[..., 1].mean(), center[..., 2].mean()
    assert blue > 150, f"expected blue-dominant client, got B={blue:.0f}"
    assert red < 90 and green < 90, f"expected pure blue, got G={green:.0f} R={red:.0f}"


def _screen_grab(rect: tuple[int, int, int, int]) -> np.ndarray:
    """BitBlt the desktop at ``rect`` -- i.e. whatever is *visible* there."""
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    screen_dc = win32gui.GetDC(0)
    src = win32ui.CreateDCFromHandle(screen_dc)
    mem = src.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(src, w, h)
    prev = mem.SelectObject(bmp)
    try:
        mem.BitBlt((0, 0), (w, h), src, (left, top), win32con.SRCCOPY)
        return np.frombuffer(bmp.GetBitmapBits(True), np.uint8).reshape(h, w, 4)[:, :, :3].copy()
    finally:
        mem.SelectObject(prev)
        win32gui.DeleteObject(bmp.GetHandle())
        mem.DeleteDC()
        src.DeleteDC()
        win32gui.ReleaseDC(0, screen_dc)


def _center_bgr(frame: np.ndarray) -> tuple[float, float, float]:
    h, w, _ = frame.shape
    c = frame[h // 3 : 2 * h // 3, w // 3 : 2 * w // 3]
    return c[..., 0].mean(), c[..., 1].mean(), c[..., 2].mean()


def test_capture_ignores_occlusion():
    # The reason for PrintWindow over a screen grab: a window fully hidden behind
    # another must still capture its OWN content. The test is self-validating -- the
    # BitBlt screen-grab asserts the cover really occludes (returns red), so the
    # PrintWindow-returns-blue assertion genuinely proves occlusion-independence.
    title = "capture-probe-occ-2a6f"
    with probe_window("#0000ff", title, size="320x240+240+200") as (root, info):
        cover = tk.Toplevel(root)
        cover.geometry("380x300+210+170")  # fully blankets the probe
        cover.configure(bg="#ff0000")
        cover.overrideredirect(True)
        cover.attributes("-topmost", True)
        cover.lift()
        # Confirm the cover has actually painted red over the probe BEFORE capturing,
        # so the blue assertion can't pass merely because the cover was late and a
        # screen-grab regression would have read the still-uncovered probe.
        vis_blue = vis_red = 0.0
        for _ in range(60):
            root.update_idletasks()
            root.update()
            time.sleep(0.02)
            vis_blue, _, vis_red = _center_bgr(_screen_grab(info.rect))
            if vis_red > 150 and vis_blue < 90:
                break
        else:
            pytest.fail(f"cover never occluded the probe; B={vis_blue:.0f} R={vis_red:.0f}")
        frame = capture.capture_window(info)

    assert not capture.is_blank(frame)
    blue, _, red = _center_bgr(frame)
    assert blue > 150 and red < 90, (
        f"occluded probe must capture its own blue content, not the red cover; "
        f"B={blue:.0f} R={red:.0f}"
    )


def test_capture_preserves_vertical_orientation():
    # Solid-color probes can't catch a vertically flipped reshape (top-down vs
    # bottom-up DIB) -- every assertion would pass mirrored. A red-over-blue split
    # pins the orientation: breaking reshape(height, width, 4) swaps the bands.
    title = "capture-probe-orient-5c1b"
    windows.enable_dpi_awareness()
    win = tk.Toplevel(_root())
    win.title(title)
    win.geometry("320x260")
    tk.Frame(win, bg="#ff0000").place(relx=0, rely=0, relwidth=1, relheight=0.5)
    tk.Frame(win, bg="#0000ff").place(relx=0, rely=0.5, relwidth=1, relheight=0.5)
    for _ in range(20):
        win.update_idletasks()
        win.update()
        time.sleep(0.01)
    try:
        info = windows.resolve_target(title=title)
        frame = capture.capture_window(info)
    finally:
        win.destroy()

    h, w, _ = frame.shape
    top = frame[int(0.28 * h) : int(0.40 * h), w // 4 : 3 * w // 4]
    bottom = frame[int(0.72 * h) : int(0.85 * h), w // 4 : 3 * w // 4]
    assert top[..., 2].mean() > 140 and top[..., 0].mean() < 100, (
        f"top band must be red; got B={top[..., 0].mean():.0f} R={top[..., 2].mean():.0f}"
    )
    assert bottom[..., 0].mean() > 140 and bottom[..., 2].mean() < 100, (
        f"bottom band must be blue; got B={bottom[..., 0].mean():.0f} R={bottom[..., 2].mean():.0f}"
    )


def test_crop_region_extracts_exact_area():
    # A white block on black at (x=60,y=30,w=50,h=20): cropping that exact rect must
    # return all-white at the right shape; a swapped x/y or off-by-one slice fails.
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    frame[30:50, 60:110] = (255, 255, 255)
    crop = capture.crop_region(frame, (60, 30, 50, 20))
    assert crop.shape == (20, 50, 3)
    assert int(crop.min()) == 255
    left_of_block = capture.crop_region(frame, (0, 30, 50, 20))
    assert int(left_of_block.max()) == 0


def test_crop_region_out_of_bounds_raises():
    # A region extending past the frame must fail loudly, not silently clamp.
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    with pytest.raises(capture.CaptureError):
        capture.crop_region(frame, (180, 0, 50, 10))  # x+w = 230 > 200


def test_is_unobscured_detects_covering_window():
    # Safety-critical direction: a window with another window on top of it must read
    # as obscured, so the BitBlt fallback never screen-reads a covered window and
    # returns the covering pixels.
    title = "capture-probe-unobsc-7d2a"
    with probe_window("#445566", title, size="300x220+260+220") as (root, info):
        cover = tk.Toplevel(root)
        cover.geometry("360x280+230+190")
        cover.configure(bg="#aa0000")
        cover.overrideredirect(True)
        cover.attributes("-topmost", True)
        cover.lift()
        for _ in range(20):
            root.update_idletasks()
            root.update()
            time.sleep(0.01)
        # Covered probe reads obscured; the topmost cover itself (nothing above it)
        # reads unobscured -- pinning both z-order directions.
        cover_hwnd = win32gui.GetAncestor(cover.winfo_id(), 2)  # GA_ROOT
        assert windows.is_unobscured(info.hwnd) is False
        assert windows.is_unobscured(cover_hwnd) is True


def test_revalidate_rejects_dead_handle():
    # A live window passes; a handle whose window was destroyed (and possibly reused)
    # must be rejected rather than captured as some other window.
    title = "capture-probe-reval-3f9e"
    with probe_window("#334455", title) as (_root, info):
        capture._revalidate(info)  # alive: must not raise
        # Same live handle, wrong expected pid -> handle was recycled; must reject.
        import dataclasses

        recycled = dataclasses.replace(info, pid=info.pid + 999_999)
        with pytest.raises(capture.CaptureError):
            capture._revalidate(recycled)
    with pytest.raises(capture.CaptureError):
        capture._revalidate(info)  # destroyed


def test_fallback_upgrades_blank_via_bitblt(monkeypatch):
    # The reason the fallback exists: a blank PrintWindow result on a safe-to-grab
    # window is upgraded to the non-blank BitBlt result. Collaborators are stubbed so
    # the upgrade branch itself is exercised (it never fires on real test windows,
    # which render fine).
    blank = np.zeros((10, 10, 3), dtype=np.uint8)
    content = np.zeros((10, 10, 3), dtype=np.uint8)
    content[5, 5] = (255, 255, 255)
    monkeypatch.setattr(capture, "_capture_hwnd", lambda hwnd, rect: blank)
    monkeypatch.setattr(capture, "_can_screen_grab", lambda hwnd: True)
    monkeypatch.setattr(capture, "_bitblt_capture", lambda rect: content)
    monkeypatch.setattr(capture.win32gui, "GetWindowRect", lambda hwnd: (0, 0, 10, 10))
    monkeypatch.setattr(capture.win32gui, "IsWindow", lambda hwnd: True)
    result = capture._capture_with_fallback(123, (0, 0, 10, 10), fallback=True)
    assert result is content


def test_capture_stream_yields_exactly_count_frames():
    # The stream must stop after `count` frames. The safety break catches a broken
    # bound (which would otherwise stream forever and hang the test).
    title = "capture-probe-stream-8c4f"
    with probe_window("#3366cc", title) as (_root, info):
        frames = []
        for frame in capture.capture_stream(info, fps=60, count=3):
            frames.append(frame)
            if len(frames) > 10:
                break
        assert len(frames) == 3
        assert all(f.ndim == 3 and f.shape[2] == 3 for f in frames)


def test_wait_for_window_times_out_after_polling():
    # A target that never appears must raise after polling the full timeout, not
    # fail instantly (which would mean the wait loop isn't actually waiting).
    start = time.monotonic()
    with pytest.raises(windows.TargetNotFound):
        capture.wait_for_window(title="no-such-window-zxqw-9f3", timeout=0.4, interval=0.1)
    assert time.monotonic() - start >= 0.4


def test_wait_for_window_returns_present_window():
    title = "capture-probe-wait-5a2c"
    with probe_window("#557799", title) as (_root, info):
        found = capture.wait_for_window(title=title, timeout=2.0)
        assert found.hwnd == info.hwnd


def test_record_writes_mp4_with_frame_count(tmp_path):
    # --record must produce a playable mp4 with at least the captured-frame count.
    # Real-time padding only ever duplicates frames, never drops them, so a count below
    # the stream length means a broken size-lock (VideoWriter rejects mismatched frames)
    # or a skipped write.
    title = "capture-probe-rec-8e1d"
    out = str(tmp_path / "rec.mp4")
    args = argparse.Namespace(
        title=title, hwnd=None, pid=None, exact=False, wait=None, loop=20.0, count=5,
        duration=None, out=None, no_save=False, format="png", region=None, record=out,
    )
    with probe_window("#224466", title) as (_root, _info):
        assert capture._run_stream(args) == 0
    reader = cv2.VideoCapture(out)
    frame_count = int(reader.get(cv2.CAP_PROP_FRAME_COUNT))
    reader.release()
    assert frame_count >= 5


def test_frames_due_pads_lag_to_realtime():
    # Real-time recording: when PrintWindow lags, the current frame is written enough
    # times to fill the wall-clock gap, so the mp4's frame count tracks elapsed*fps and
    # the video plays at real speed instead of fast.
    fps = 20.0
    # First frame at t=0: exactly one write, nothing to pad yet.
    assert capture._frames_due(0.0, fps, 0) == 1
    # Keeping up (one interval elapsed, one frame already written): still one write.
    assert capture._frames_due(0.05, fps, 1) == 1
    # A 0.3s stall after 2 frames written -> 6 frames due by now -> pad 4 duplicates.
    assert capture._frames_due(0.30, fps, 2) == 4
    # Never drops below one even if somehow already ahead of the clock.
    assert capture._frames_due(0.0, fps, 10) == 1


def test_capture_stream_count_zero_yields_nothing():
    # The bound is checked before capturing, so count=0 produces no frames (a
    # post-yield check would leak exactly one).
    title = "capture-probe-stream0-1c7e"
    with probe_window("#3366cc", title) as (_root, info):
        frames = list(capture.capture_stream(info, fps=60, count=0))
        assert frames == []


def test_capture_stream_rejects_nonpositive_fps():
    # fps<=0 would make a 1/fps interval division blow up or spin unpaced; reject it.
    with pytest.raises(ValueError):
        next(capture.capture_stream(title="anything", fps=0))


def test_wait_for_window_does_not_overshoot_deadline():
    # The per-poll sleep is clamped to the remaining budget, so a large interval
    # cannot blow past a small timeout (here ~0.3s, not the 10s interval).
    start = time.monotonic()
    with pytest.raises(windows.TargetNotFound):
        capture.wait_for_window(title="no-such-window-overshoot-2b", timeout=0.3, interval=10.0)
    assert time.monotonic() - start < 3.0


def test_capture_stream_rejects_minimized():
    # Streaming a minimized window would hold focus for the whole run, so it must be
    # refused outright rather than restored.
    title = "capture-probe-streammin-2e1b"
    with probe_window("#3366cc", title) as (root, info):
        min_info = _minimize(root, info)
        with pytest.raises(capture.MinimizedWindowError):
            next(capture.capture_stream(min_info, fps=10, count=1))


def test_resolve_requires_a_selector():
    # An empty selector would silently resolve to the largest desktop window; the
    # library entry must reject it rather than capture an arbitrary window.
    with pytest.raises(ValueError):
        windows.resolve_target()


def test_maximized_restore_round_trips_through_capture():
    # A maximized-then-minimized window must still carry "restore to maximized" after
    # an offscreen capture. Windows preserves this through our restore/minimize
    # sequence (no code of ours implements it), so this is a forward guard: it catches
    # any future change to _capture_minimized that un-maximizes the window.
    WPF_RESTORETOMAXIMIZED = 0x2
    title = "capture-probe-max-9d4a"
    with probe_window("#446688", title) as (win, info):
        win.state("zoomed")
        for _ in range(15):
            win.update()
            time.sleep(0.01)
        assert windows.window_state(info.hwnd) == WindowState.MAXIMIZED
        min_info = _minimize(win, info)
        assert win32gui.GetWindowPlacement(info.hwnd)[0] & WPF_RESTORETOMAXIMIZED
        capture.capture_window(min_info, restore_minimized=True, offscreen=True)
        for _ in range(10):
            win.update()
            time.sleep(0.01)
        assert win32gui.GetWindowPlacement(info.hwnd)[0] & WPF_RESTORETOMAXIMIZED


@pytest.mark.parametrize("offscreen", [True, False])
def test_topmost_restore_preserves_topmost(offscreen):
    # BOTH restore paths send the window to HWND_BOTTOM, which strips always-on-top;
    # the capture must reinstate it on each (the offscreen path is the default, so a
    # missing restore there would silently demote topmost windows on the common path).
    title = f"capture-probe-top-{'off' if offscreen else 'in'}-3e7b"
    with probe_window("#228822", title) as (win, info):
        win.attributes("-topmost", True)
        for _ in range(15):
            win.update()
            time.sleep(0.01)
        assert windows.is_topmost(info.hwnd)
        min_info = _minimize(win, info)
        capture.capture_window(min_info, restore_minimized=True, offscreen=offscreen)
        for _ in range(10):
            win.update()
            time.sleep(0.01)
        assert windows.is_topmost(info.hwnd) is True


def _ledger_entry(info, owner_pid, owner_creation, placement=None):
    return {
        "hwnd": info.hwnd,
        "pid": info.pid,
        "class": info.class_name,
        "placement": windows._placement_to_list(
            placement or win32gui.GetWindowPlacement(info.hwnd)
        ),
        "was_topmost": False,
        "exstyle": None,
        "owner_pid": owner_pid,
        "owner_creation": owner_creation,
    }


def test_reconcile_recovers_dead_owner_strand(tmp_path, monkeypatch):
    # A window parked by a now-dead capture process is stranded; reconcile must put it
    # back (placement restored, re-minimized) and clear the ledger entry.
    monkeypatch.setattr(windows, "_ledger_path", lambda: str(tmp_path / "parked.json"))
    title = "capture-probe-recon-7a1f"
    with probe_window("#665544", title) as (win, info):
        saved = win32gui.GetWindowPlacement(info.hwnd)
        windows.restore_background(info.hwnd, offscreen=True)  # simulate the strand
        for _ in range(10):
            win.update()
            time.sleep(0.01)
        assert windows.window_state(info.hwnd) != WindowState.MINIMIZED
        windows._write_ledger([_ledger_entry(info, 999999, None, placement=saved)])

        recovered = windows.reconcile(restore=True)
        for _ in range(10):
            win.update()
            time.sleep(0.01)
        assert len(recovered) == 1
        assert windows.window_state(info.hwnd) == WindowState.MINIMIZED
        assert windows._read_ledger() == []


def test_reconcile_leaves_live_owner_untouched(tmp_path, monkeypatch):
    # Safety-critical: a window whose owner is still alive may be mid-capture, so
    # reconcile must NOT touch it or drop its entry (a false strand recovery would
    # yank a window a live process is using).
    monkeypatch.setattr(windows, "_ledger_path", lambda: str(tmp_path / "parked.json"))
    title = "capture-probe-recon-live-3b2e"
    with probe_window("#445566", title) as (win, info):
        windows.restore_background(info.hwnd, offscreen=True)
        for _ in range(10):
            win.update()
            time.sleep(0.01)
        creation = windows._process_creation_time(os.getpid())
        windows._write_ledger([_ledger_entry(info, os.getpid(), creation)])

        recovered = windows.reconcile(restore=True)
        assert recovered == []
        assert windows.window_state(info.hwnd) != WindowState.MINIMIZED
        assert len(windows._read_ledger()) == 1


def test_owner_is_dead_detects_pid_reuse():
    # A live PID whose creation time differs from what we recorded means the original
    # owner is gone and its PID was reused -> dead (the PID-reuse defense). Without
    # this, a reused PID would read as the live owner and block recovery.
    assert windows._owner_is_dead(os.getpid(), "0000-00-00 stale-creation") is True
    # Same PID, matching creation -> the actual live owner -> not dead.
    real = windows._process_creation_time(os.getpid())
    assert windows._owner_is_dead(os.getpid(), real) is False


def test_reconcile_survives_malformed_ledger(tmp_path, monkeypatch):
    # A corrupt entry must not crash count_stranded (runs on every capture) or
    # reconcile; the tool would otherwise be bricked until the file is deleted by hand.
    monkeypatch.setattr(windows, "_ledger_path", lambda: str(tmp_path / "parked.json"))
    windows._write_ledger([
        "not-a-dict",
        {"owner_pid": "a-string-not-int", "hwnd": 1},  # OpenProcess would TypeError
        {"owner_pid": 999999, "owner_creation": None, "hwnd": 123, "placement": [1]},  # short placement
    ])
    assert windows.count_stranded() == 0
    assert windows.reconcile(restore=True) == []  # no crash; bad entries dropped


def test_ambiguous_title_is_refused(capsys):
    # A --title matching more than one window must refuse rather than silently grab
    # one (which could be a sensitive window the user never meant to capture).
    with probe_window("#112233", "capture-ambig-aaa-6c2d"):
        with probe_window("#445566", "capture-ambig-bbb-6c2d"):
            assert capture.main(["--title", "capture-ambig", "--no-save"]) == 1
            assert "2 windows match" in capsys.readouterr().err


def test_exact_title_resolves_single_of_several():
    # --exact is the advertised remedy. B's title CONTAINS A's, so the substring
    # "capture-exq-7d3e" matches both (refused) while --exact matches only A -- so this
    # genuinely exercises the exact threading (a substring fallback would stay ambiguous).
    with probe_window("#112233", "capture-exq-7d3e"):
        with probe_window("#445566", "capture-exq-7d3e-more"):
            assert capture.main(["--title", "capture-exq-7d3e", "--no-save"]) == 1  # ambiguous
            assert capture.main(["--title", "capture-exq-7d3e", "--exact", "--no-save"]) == 0


def test_ocr_without_venv_raises_helpful_error(monkeypatch):
    # OCR runs in a separate 3.13 venv that isn't part of the repo; if it's missing,
    # the error must point the user at the setup, not crash spawning a missing python.
    monkeypatch.setattr(capture, "_ocr_python", lambda: r"C:\no\such\ocr-venv\python.exe")
    frame = np.zeros((20, 40, 3), dtype=np.uint8)
    with pytest.raises(capture.OcrError) as exc:
        capture._run_ocr(frame, "en")
    assert "venv" in str(exc.value).lower()


def test_ocr_surfaces_structured_error_not_stderr_noise(monkeypatch):
    # A worker failure writes {"error": ...} to stdout AND exits nonzero with noisy
    # stderr (paddle chatter). The core must surface the structured error, not the
    # noise -- otherwise every OCR failure reports the same useless message.
    monkeypatch.setattr(capture, "_ocr_python", lambda: __file__)  # exists -> reach subprocess

    class _FakeProc:
        returncode = 1
        stdout = b'{"error": "could not decode the image"}'
        stderr = b"W0620 paddle glog noise oneDNN v3.6.2 ..."

    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **k: _FakeProc())
    with pytest.raises(capture.OcrError) as exc:
        capture._run_ocr(np.zeros((20, 40, 3), dtype=np.uint8), "en")
    assert "could not decode" in str(exc.value)
    assert "glog noise" not in str(exc.value)


def test_capture_dead_window_raises_clean_error():
    # A handle resolved then closed before capture must surface a typed CaptureError,
    # not a raw GDI failure from GetWindowDC on a dead handle.
    title = "capture-probe-dead-6f0e"
    with probe_window("#202020", title) as (_root, info):
        pass  # window is destroyed on exit, info.hwnd now dangles
    with pytest.raises(capture.CaptureError):
        capture.capture_window(info)


def _minimize(root, info):
    root.iconify()
    for _ in range(15):
        root.update()
        time.sleep(0.01)
    min_info = windows.window_info(info.hwnd)
    assert min_info.state == WindowState.MINIMIZED
    return min_info


def test_capture_minimized_requires_opt_in():
    # Un-minimizing briefly steals focus on Windows, so the default must refuse a
    # minimized target rather than disrupt the user; opt-in is restore_minimized=True.
    title = "capture-probe-min-4e8d"
    with probe_window("#00ff00", title) as (root, info):
        min_info = _minimize(root, info)
        with pytest.raises(capture.MinimizedWindowError):
            capture.capture_window(min_info)


def test_minimized_restore_reminimizes_and_returns_frame():
    # Opted-in capture must restore -> capture -> re-minimize, leaving the window
    # minimized again (the finally re-minimize guard). Content correctness of the
    # restored frame is verified empirically against a real app (mspaint); a tkinter
    # probe can't repaint while this thread blocks in capture, so only orchestration
    # and the re-minimize guarantee are asserted here.
    title = "capture-probe-restore-1d7a"
    with probe_window("#00ccff", title) as (root, info):
        min_info = _minimize(root, info)
        frame = capture.capture_window(min_info, restore_minimized=True, offscreen=False)
        assert frame.ndim == 3 and frame.shape[2] == 3
        assert frame.shape[0] > 0 and frame.shape[1] > 0
        assert windows.window_state(info.hwnd) == WindowState.MINIMIZED


def test_offscreen_restore_moves_offscreen_then_restores_placement():
    # Off-screen mode must (a) actually relocate the window past the virtual desktop
    # while capturing, and (b) put rcNormalPosition back afterward so the user's next
    # manual restore doesn't land off-screen. A spy on _capture_hwnd records the live
    # rect at capture time -- equality-only checks would pass even if move_offscreen
    # were removed (placement would simply never change).
    title = "capture-probe-offscreen-3b9c"
    with probe_window("#ff8800", title) as (root, info):
        min_info = _minimize(root, info)
        before = win32gui.GetWindowPlacement(info.hwnd)[4]
        captured_rect = {}
        original = capture._capture_hwnd

        def spy(hwnd, rect):
            captured_rect["live"] = win32gui.GetWindowRect(hwnd)
            return original(hwnd, rect)

        capture._capture_hwnd = spy
        try:
            capture.capture_window(min_info, restore_minimized=True, offscreen=True)
        finally:
            capture._capture_hwnd = original
        for _ in range(10):
            root.update()
            time.sleep(0.01)
        after = win32gui.GetWindowPlacement(info.hwnd)[4]

        right_edge = windows.virtual_screen_bounds()[2]
        assert captured_rect["live"][0] >= right_edge, (
            f"window not off-screen during capture: {captured_rect['live']} vs right={right_edge}"
        )
        assert after == before, f"normal position not restored: {before} -> {after}"
