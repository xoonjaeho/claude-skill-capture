"""Optional high-fidelity recording backend (subprocess shim over Windows.Graphics.Capture).

The actual capture+encode runs in wgc_record_worker.py inside a dedicated .venv-wgc, so
``windows-capture`` stays out of the core interpreter -- the same isolation the OCR worker
uses. WGC reads a window's own DWM compositor surface, so an occluded/background window
records correctly, focus-safe, at paint-rate on the GPU (no PrintWindow vsync block or black
GPU-window frame). The worker samples the latest frame at the target fps and encodes it to
mp4 (cv2/mp4v), padding to wall-clock so playback is real-time.

capture.py lazy-imports this module only for ``--record``; if the .venv-wgc is absent the
core skill is unaffected and recording falls back to PrintWindow + mp4v.
"""
from __future__ import annotations

import json
import os
import subprocess


class WgcError(Exception):
    """WGC recording could not run (worker failed, no frame produced, or output failed)."""


class WgcUnavailable(WgcError):
    """The dedicated .venv-wgc (with ``windows-capture``) is not set up."""


def _wgc_python() -> str:
    """Path to the .venv-wgc interpreter. Config-fixed relative to this file -- never
    PATH-resolved, which would find the core interpreter that has no windows-capture."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", ".venv-wgc", "Scripts", "python.exe"))


def _worker() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "wgc_record_worker.py")


_available: bool | None = None


def available() -> bool:
    """True if the .venv-wgc backend is set up AND windows-capture imports in it.

    Probes the import (once, cached) rather than only checking the interpreter exists, so a
    present-but-broken venv falls back to PrintWindow+mp4v instead of hard-failing inside the
    worker -- capture.py routes to WGC only when this returns True.
    """
    global _available
    if _available is not None:
        return _available
    py = _wgc_python()
    if not os.path.exists(py):
        _available = False
        return _available
    try:
        proc = subprocess.run(
            [py, "-c", "import windows_capture"], capture_output=True, timeout=30
        )
        _available = proc.returncode == 0
    except (subprocess.SubprocessError, OSError):
        _available = False
    return _available


def _cleanup(path: str) -> None:
    """Best-effort removal of a partial/corrupt output left by a killed or failed worker."""
    try:
        os.unlink(path)
    except OSError:
        pass


def record_window(
    hwnd: int,
    out_path: str,
    *,
    fps: float,
    duration: float | None = None,
    count: int | None = None,
    region: tuple[int, int, int, int] | None = None,
    cursor: bool = False,
) -> int:
    """Record window ``hwnd`` to ``out_path`` (mp4) via the .venv-wgc worker. Returns frames
    written. Stops after ``duration`` seconds or ``count`` frames (both None -> 30 frames).

    Raises WgcUnavailable if the venv is not set up, WgcError if the worker fails, produces
    no frame (e.g. a minimized/protected window), or the output cannot be written.
    """
    worker_python = _wgc_python()
    if not os.path.exists(worker_python):
        raise WgcUnavailable(
            f"WGC venv not found at {worker_python}. Set it up: "
            "py -3.14 -m venv .venv-wgc && "
            ".venv-wgc/Scripts/python -m pip install -r requirements-wgc.txt"
        )
    argv = [worker_python, _worker(), "--hwnd", str(hwnd), "--out", out_path, "--fps", str(fps)]
    if duration is not None:
        argv += ["--duration", str(duration)]
    if count is not None:
        argv += ["--count", str(count)]
    if region is not None:
        argv += ["--region", ",".join(str(v) for v in region)]
    if cursor:
        argv += ["--cursor"]

    # Generous timeout: first-frame wait + the requested record time + worker startup.
    record_s = duration if duration is not None else (count if count is not None else 30) / fps
    timeout = 5.0 + record_s + 30.0
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # A killed worker never finalized the mp4 moov -> leave no corrupt file behind.
        _cleanup(out_path)
        raise WgcError(f"WGC worker timed out after {timeout:.0f}s")

    # Prefer the worker's structured JSON on stdout over its exit code: a clean failure
    # message lands there, while native logging goes to stderr.
    try:
        data = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict) and "error" in data:
        _cleanup(out_path)  # error -> no (partial) file, mirroring "no frames -> no file"
        raise WgcError(data["error"])
    if isinstance(data, dict) and "frames" in data:
        return data["frames"]
    _cleanup(out_path)
    raise WgcError(
        f"WGC worker failed (exit {proc.returncode}): "
        f"{proc.stderr.decode('utf-8', 'replace').strip()[:500]}"
    )
