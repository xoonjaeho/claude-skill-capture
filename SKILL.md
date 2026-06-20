---
name: capture
description: Capture any Windows window to a PNG and/or an OpenCV BGR ndarray, even when the window is occluded behind other windows or minimized — focus-safe (nothing is moved or activated). Capturing a minimized window is opt-in because un-minimizing it briefly changes the foreground window. Use when the user wants to screenshot or grab a specific application window (editor, tool, dev build) for inspection, diffing, or OpenCV processing. Windows only; needs pywin32 + numpy + opencv-python on Python 3.14.
---

# /capture — per-window screen capture (occluded & minimized)

Capture a specific window's pixels to a PNG and/or a BGR `numpy.ndarray` ready for
OpenCV — without bringing the window to the foreground.

## How it works

`PrintWindow(hwnd, dc, PW_RENDERFULLCONTENT)` asks the window to render itself into
an off-screen device context. Unlike a screen grab (which reads whatever is visible
on the monitor), this reads the window's *own* content, so a window hidden behind
others captures correctly with **zero side effects**.

If PrintWindow returns a blank frame for an unobscured, on-screen window, a screen-read
(BitBlt) is tried automatically and used only if better — so a returned uniform frame
means both paths found nothing (a protected or genuinely solid window).

## Setup

Run on the global **Python 3.14** interpreter (Windows). Install the core deps once:

```
pip install -r requirements.txt
```

The CLI exits with a clear `pip install -r requirements.txt` message if a dependency
is missing, rather than a raw traceback.

## Two capabilities, two guarantees

- **Occluded / background / foreground window (not minimized): focus-safe.** No
  window is moved, shown, or activated. This is the default and the common case.
- **Minimized window: opt-in.** DWM does not composite a minimized window, so it
  must be briefly un-minimized to capture. On Windows 11 that un-minimize forces the
  window to the foreground and the OS foreground lock prevents handing focus back —
  an unavoidable ~0.2s focus change. So minimized capture requires an explicit
  opt-in flag, and is restored **off-screen by default** so the window does not
  visibly flash (only ~1 frame at restore).

## CLI usage

```
python ~/.claude/skills/capture/scripts/capture.py [options]
```

### Selecting the window

| Flag | Meaning |
|------|---------|
| `--title "text"` | partial, case-insensitive title match; **refused if it matches more than one window** (use `--hwnd`/`--exact`) |
| `--exact` | match `--title` exactly, not as a substring |
| `--hwnd N` | exact window handle |
| `--pid N` | the process's visible window (refused if it has >1; use `--hwnd`) |
| `--wait [S]` | poll up to S seconds (default 30) for the target window to appear |
| `--list` | print visible windows (HWND, PID, size, state, title) and exit |

### Output

| Flag | Meaning |
|------|---------|
| `--out PATH` | output image path (default: auto-named in the current directory) |
| `--region X,Y,W,H` | crop to a region in pixels of the capture (top-left origin) |
| `--format png\|jpg` | format for the auto-named file (default: png) |
| `--no-save` | don't write a file; just report the capture (size, blank?) |

### Continuous capture (streaming)

| Flag | Meaning |
|------|---------|
| `--loop FPS` | stream a **non-minimized** window at ~FPS frames/sec (focus-safe) |
| `--count N` | stop after N frames (default 30, unless `--duration` is given) |
| `--duration S` | stop after S seconds |
| `--record PATH` | record the stream to an mp4 at the --loop fps (plays fast if capture can't keep up) |

Streaming only works on a non-minimized window (it never moves or activates it). A
minimized target is refused — restore it first. Throughput is PrintWindow-bound and
size-dependent (~15 fps for a ~2000×2000 window, faster for smaller ones).
`--record` requires `--loop` and cannot be combined with `--out`/`--no-save`.

### Text extraction (OCR, optional)

| Flag | Meaning |
|------|---------|
| `--ocr` | OCR the capture and print the recognized text |
| `--lang L` | OCR language (default `en`; e.g. `korean`, `ch`, `japan`) |

OCR (PaddleOCR) runs in a separate Python 3.13 venv (`.venv-ocr/`), set up once with
`py -3.13 -m venv .venv-ocr && .venv-ocr/Scripts/python -m pip install -r
requirements-ocr.txt`. Pair `--ocr` with `--region` to read a specific area. The core
stays on Python 3.14; if the venv isn't set up, `--ocr` reports how to create it.
`--ocr` prints the recognized text and, unless `--out` is also given, does **not** save
an image.

### Minimized handling (opt-in)

| Flag | Meaning |
|------|---------|
| `--restore-minimized` | allow briefly un-minimizing a minimized target (short focus change) |
| `--in-place` | with the above, restore at the real position (~brief visible flash) instead of off-screen |
| `--stealth` | with the above, also hide the taskbar button during the restore |

A minimized target **without** `--restore-minimized` exits with a message explaining
the options. The window is always re-minimized and put back where it was afterward.

If a capture is killed mid-restore (rare), the window can be left stranded
off-screen. `capture.py --reconcile` recovers any such window (and a normal capture
prints a one-line notice when one exists). This never touches a window a live capture
is still using.

### Examples

```
# Find a target
python ~/.claude/skills/capture/scripts/capture.py --list

# Capture a window that is behind others (focus-safe, no side effects)
python ~/.claude/skills/capture/scripts/capture.py --title "Unreal Editor" --out shot.png

# Capture a minimized window (accepting the brief focus change), off-screen
python ~/.claude/skills/capture/scripts/capture.py --title "Output Log" --restore-minimized --out log.png
```

## Library API (for OpenCV workflows)

```python
import os, sys
sys.path.insert(0, os.path.expanduser("~/.claude/skills/capture/scripts"))
import capture

frame = capture.capture_window(title="Unreal Editor")   # BGR ndarray, ready for cv2
```

`capture_window` accepts `title=`/`pid=`, an hwnd int, or a resolved `WindowInfo`,
plus the same `restore_minimized` / `offscreen` / `stealth` keyword options as the
CLI. It returns an OpenCV-ready BGR `ndarray` and raises `CaptureError` (or
`MinimizedWindowError`) on failure.

## Limitations

- **Windows only** — uses Win32 / DWM APIs.
- **Black frame on protected content** — exclusive-fullscreen games, DRM video, and
  some hardware-overlay surfaces are not composited by DWM and return a uniform
  (usually black) frame. A perfectly uniform frame is the "nothing was rendered"
  signal, not a bug.
- **No cursor** — PrintWindow does not include the mouse cursor.
- **Development-process use** — intended for editor / tool / dev-build windows, not
  anti-cheat-protected live games.

## Guidance for the agent

- Default to the focus-safe path. Only pass `--restore-minimized` when the target is
  actually minimized **and** the user accepts the brief focus change — surface that
  trade-off first rather than minimizing the user's window silently.
- Use `--list` when the target window isn't found by title.
- For OpenCV pipelines (template matching, frame diff, color masking), import the
  library API and work on the returned ndarray instead of re-reading the PNG.
- **OCR is optional and not installed by default.** Core capture never needs it. When
  the user asks to read text and `--ocr` reports the venv is missing, *offer* to set it
  up (see the OCR section) — never install silently; it pulls a large PaddleOCR download.
