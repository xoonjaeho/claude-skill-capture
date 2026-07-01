"""WGC recording worker -- runs in the dedicated .venv-wgc so windows-capture stays out
of the core interpreter (mirrors the OCR worker pattern).

Captures a window by hwnd via Windows.Graphics.Capture and encodes to mp4 (cv2/mp4v),
then writes one JSON object to stdout: ``{"frames": N}`` on success or ``{"error": "..."}``
on failure. Invoked by wgc_record.record_window() as a subprocess; not meant to be run by
hand. windows-capture's native logging is redirected off real stdout so only the JSON lands
there.
"""
# windows-capture lives in .venv-wgc, not the core env that type-checks this.
# pyright: reportMissingImports=false
import argparse
import json
import os
import sys
import time


def _frames_due(elapsed: float, fps: float, written: int) -> int:
    """Writes owed to keep the mp4's clock on wall-clock (pad the encoder's lag with
    duplicates). At least 1 so a frame is never dropped. Mirrors capture._frames_due."""
    return max(1, round(elapsed * fps) - written)


def _crop(frame, region, np):
    x, y, w, h = region
    fh, fw = frame.shape[:2]
    if w <= 0 or h <= 0:
        raise ValueError(f"region size must be positive, got {w}x{h}")
    if x < 0 or y < 0 or x + w > fw or y + h > fh:
        raise ValueError(f"region {x},{y},{w},{h} is outside the {fw}x{fh} capture")
    # A bare slice is a non-contiguous view cv2.VideoWriter can mishandle.
    return np.ascontiguousarray(frame[y : y + h, x : x + w])


def _record(hwnd, out, fps, duration, count, region, cursor, first_frame_timeout=5.0):
    import cv2
    import numpy as np
    from windows_capture import WindowsCapture

    if fps <= 0:
        raise ValueError("fps must be positive")
    if count is None and duration is None:
        count = 30

    latest: dict = {}
    cap = WindowsCapture(window_hwnd=hwnd, cursor_capture=cursor, draw_border=False)

    @cap.event
    def on_frame_arrived(frame, capture_control):
        # .copy() detaches from WGC's recycled frame memory; latest-wins single slot,
        # atomic under the GIL, so the sampling loop needs no lock.
        latest["frame"] = frame.convert_to_bgr().frame_buffer.copy()

    @cap.event
    def on_closed():
        latest["closed"] = True

    control = cap.start_free_threaded()
    writer = None
    try:
        deadline = time.monotonic() + first_frame_timeout
        while "frame" not in latest:
            if latest.get("closed"):
                raise RuntimeError("capture session closed before any frame (window gone?)")
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"no frame within {first_frame_timeout}s "
                    "(window minimized, protected, or not composited?)"
                )
            time.sleep(0.005)

        size = None
        interval = 1.0 / fps
        written = 0
        rec_start = time.monotonic()  # anchor the mp4 clock at the first arrived frame
        while True:
            if count is not None and written >= count:
                break
            if duration is not None and time.monotonic() - rec_start >= duration:
                break
            if latest.get("closed"):
                break
            tick = time.monotonic()
            frame = latest["frame"]  # atomic reference grab
            if region is not None:
                frame = _crop(frame, region, np)
            if writer is None:
                h0, w0 = frame.shape[0], frame.shape[1]
                size = (w0 - w0 % 2, h0 - h0 % 2)  # mp4 needs even dims
                writer = cv2.VideoWriter(out, cv2.VideoWriter.fourcc(*"mp4v"), fps, size)
                if not writer.isOpened():
                    raise RuntimeError(f"cannot open {out} for writing")
            if (frame.shape[1], frame.shape[0]) != size:
                frame = cv2.resize(frame, size)
            for _ in range(_frames_due(tick - rec_start, fps, written)):
                writer.write(frame)
                written += 1
            remaining = interval - (time.monotonic() - tick)
            if remaining > 0:
                time.sleep(remaining)
        return written
    finally:
        if writer is not None:
            try:
                writer.release()  # finalize the mp4 moov
            except Exception:
                pass
        try:
            control.stop()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hwnd", type=int, required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--fps", type=float, required=True)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--count", type=int)
    parser.add_argument("--region", help="x,y,w,h")
    parser.add_argument("--cursor", action="store_true")
    args = parser.parse_args()
    region = tuple(int(v) for v in args.region.split(",")) if args.region else None

    # Redirect fd 1 to fd 2 so neither Python nor windows-capture's native logging can
    # corrupt the JSON on real stdout. The saved real-stdout fd is kept for the result.
    saved_stdout_fd = os.dup(1)
    os.dup2(2, 1)
    error = None
    frames = 0
    try:
        frames = _record(
            args.hwnd, args.out, args.fps, args.duration, args.count, region, args.cursor
        )
    except Exception as exc:  # any failure -> structured error, not a traceback
        error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            sys.stdout.flush()
        except Exception:
            pass

    # Write the result straight to the saved fd WITHOUT restoring fd 1: a late native log
    # from windows-capture's background thread can't slip between a restore and the write to
    # corrupt the JSON, since fd 1 stays pointed at stderr for the process's remaining life.
    payload = {"error": error} if error else {"frames": frames}
    os.write(saved_stdout_fd, json.dumps(payload).encode("utf-8"))
    os.close(saved_stdout_fd)
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
