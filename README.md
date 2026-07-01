# /capture — per-window screen capture (occluded & minimized)

Capture a specific Windows window to a PNG and/or an OpenCV-ready BGR `numpy.ndarray`
— **even when the window is hidden behind others or minimized** — without bringing it
to the foreground.

A Claude Code skill and a standalone Python CLI/library. Windows only.

## Quick start

```bash
# one-time
pip install -r requirements.txt

# list visible windows
python scripts/capture.py --list

# capture a window that is behind others (focus-safe, no side effects)
python scripts/capture.py --title "Unreal Editor" --out shot.png
```

```python
# or as a library, straight into OpenCV
import sys; sys.path.insert(0, "scripts")
import capture, cv2

frame = capture.capture_window(title="Unreal Editor")   # BGR ndarray
cv2.imwrite("shot.png", frame)
```

## How it works

`PrintWindow(hwnd, dc, PW_RENDERFULLCONTENT)` asks a window to render itself into an
off-screen device context. Unlike a screen grab (`BitBlt` of the desktop, which
returns whatever is *visible*), this reads the window's **own** content — so a window
fully covered by other windows still captures correctly, with no window manipulation
at all.

Two capabilities with two different guarantees:

| Target | Behavior | Focus |
|--------|----------|-------|
| Foreground / occluded / background (not minimized) | Captured directly via PrintWindow | **Focus-safe** — nothing is moved or activated |
| Minimized | Briefly un-minimized (off-screen by default), captured, re-minimized | **Opt-in** — un-minimizing forces a ~0.2s foreground change on Windows that cannot be prevented |

DWM does not composite a minimized window, so it must be un-minimized to have
content. On Windows 11 the un-minimize forces the window to the foreground and the OS
foreground lock blocks handing focus back, so minimized capture is opt-in and
documented as a brief, unavoidable focus change. Off-screen restore keeps the window
from visibly flashing (it is shown for ~1 frame, then moved past the desktop edge),
and the window's original position is restored afterward.

If PrintWindow returns a blank frame for a non-minimized window that is fully on one
monitor and unobscured, an automatic screen-read (BitBlt) is tried and used only if
it is better — this recovers some GPU-rendered windows. The screen-read is never used
for an obscured window (it would return the covering pixels), and a frame that is
still blank is returned as-is with a reason hint (`blank_note`) rather than raised:
a uniform frame may be a genuinely solid or a protected window.

## Files

```
capture/
├─ SKILL.md            # Claude Code skill instructions (agent-facing)
├─ README.md           # this file
├─ requirements.txt    # core deps (pywin32, numpy, opencv-python) — Python 3.14
├─ pyrightconfig.json  # type-check config (scripts/ on path)
├─ scripts/
│  ├─ capture.py       # CLI + library: capture_window, is_blank
│  └─ windows.py       # window enumeration, resolution, DPI, restore strategy
└─ tests/
   └─ test_capture.py  # integration + unit tests (tkinter probe, R1 mutation checks)
```

## Install (as a Claude Code skill)

Clone into your skills directory so `/capture` is available:

```bash
git clone https://github.com/xoonjaeho/claude-skill-capture.git ~/.claude/skills/capture
pip install -r ~/.claude/skills/capture/requirements.txt
```

The skill runs on the global Python 3.14 interpreter. No hook or registration is
needed — `SKILL.md` tells the agent how to invoke the CLI.

## Usage

### Selecting the window

| Flag | Meaning |
|------|---------|
| `--title "text"` | partial, case-insensitive title match (refused if it matches >1 window) |
| `--exact` | match `--title` exactly, not as a substring |
| `--hwnd N` | exact window handle |
| `--pid N` | the process's visible window (refused if it has more than one; use `--hwnd`) |
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
| `--loop FPS` | stream a non-minimized window at ~FPS frames/sec (focus-safe) |
| `--count N` | stop after N frames (default 30, unless `--duration` is given) |
| `--duration S` | stop after S seconds |
| `--record PATH` | record the stream to a real-time mp4 at the --loop fps (duplicates frames to stay real-time when capture can't keep up) |

Streaming works only on a non-minimized window (it is never moved or activated; a
minimized target is refused). Throughput is PrintWindow-bound and size-dependent —
about **15 fps for a ~2000×2000 window** on the reference machine, faster for smaller
windows.

### Minimized handling (opt-in)

| Flag | Meaning |
|------|---------|
| `--restore-minimized` | allow briefly un-minimizing a minimized target (short focus change) |
| `--in-place` | restore at the real position (~brief visible flash) instead of off-screen |
| `--stealth` | also hide the taskbar button during the restore |

### Crash recovery

| Flag | Meaning |
|------|---------|
| `--reconcile` | recover any window left stranded off-screen by a killed capture |

A capture that is killed (SIGKILL/crash) during the ~0.2s it has a window un-minimized
can leave that window stranded off-screen and invisible. A write-ahead ledger records
each parked window; `--reconcile` restores any whose owning capture process is gone
(and a normal capture prints a one-line notice when strands exist). It acts only on
windows whose owner is *positively* dead, never one a live capture is still using.

### Library API

```python
import capture
from capture import CaptureError, MinimizedWindowError

frame = capture.capture_window(title="Notepad")            # BGR ndarray
frame = capture.capture_window(hwnd=0x12345)               # by handle
frame = capture.capture_window(title="Log", restore_minimized=True)  # opt-in minimized

capture.is_blank(frame)                       # True if nothing was rendered
capture.crop_region(frame, (x, y, w, h))      # crop in capture pixels

for frame in capture.capture_stream(title="Tool", fps=15, count=100):
    ...                                       # focus-safe stream of BGR frames
```

`windows.list_windows()` returns `WindowInfo` records (hwnd, pid, title, class_name,
rect, state) largest-first; `windows.resolve_target(title=/hwnd=/pid=)` resolves one.

## OpenCV recipes

`capture_window` returns a BGR `ndarray`, so anything OpenCV does works directly. The
skill supplies frames; analysis is yours.

### Find a UI element (template matching)

```python
import cv2, numpy as np, capture

frame = capture.capture_window(title="Unreal Editor")
icon = cv2.imread("play_button.png")                       # BGR template
res = cv2.matchTemplate(frame, icon, cv2.TM_CCOEFF_NORMED)
_, score, _, loc = cv2.minMaxLoc(res)
if score > 0.9:
    h, w = icon.shape[:2]
    center = (loc[0] + w // 2, loc[1] + h // 2)            # click point, etc.
```

### Detect what changed (frame diff)

```python
import cv2, capture

a = cv2.cvtColor(capture.capture_window(title="Tool"), cv2.COLOR_BGR2GRAY)
# ... time passes ...
b = cv2.cvtColor(capture.capture_window(title="Tool"), cv2.COLOR_BGR2GRAY)
delta = cv2.absdiff(a, b)
changed = cv2.countNonZero(cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)[1])
```

### Find colored regions (color masking)

```python
import cv2, numpy as np, capture

frame = capture.capture_window(title="Dashboard")
hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
mask = cv2.inRange(hsv, (40, 80, 80), (80, 255, 255))      # e.g. green "OK" status
present = cv2.countNonZero(mask) > 0
```

### Prep a region for OCR

```python
import cv2, capture

frame = capture.capture_window(title="Score Panel")
x, y, w, h = 20, 60, 200, 40                               # region of interest
roi = frame[y:y + h, x:x + w]
gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
_, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
cv2.imwrite("ocr_input.png", binary)                       # feed to an OCR engine
```

## OCR (optional)

OCR uses PaddleOCR, which has no Python 3.14 wheel, so it runs in a **separate
Python 3.13 venv** as a subprocess -- the core stays on 3.14 and the two never share
an interpreter (so paddle's numpy pin can't fight the core's numpy 2.x). Set it up
once:

```bash
py -3.13 -m venv .venv-ocr
.venv-ocr/Scripts/python -m pip install -r requirements-ocr.txt
```

Then:

```bash
python scripts/capture.py --hwnd 12345 --region 20,60,200,40 --ocr --lang en
```

```python
import capture
results = capture.capture_text(title="Score Panel", region=(20, 60, 200, 40), lang="en")
# [{"text": "...", "confidence": 0.99, "box": [[x, y], ...]}, ...]
```

The `.venv-ocr/` path is config-fixed relative to the skill (never PATH-resolved, so
it can't accidentally pick the 3.14 python). Languages: `en`, `korean`, `ch`, `japan`,
etc. The first run downloads the PP-OCR models to `%USERPROFILE%\.paddlex`; for
air-gapped/corporate use, **vendor those models offline** -- download once on a
connected machine and copy the `.paddlex` model cache to the target.

## Limitations / out-of-scope

- **Windows only** — Win32 / DWM APIs.
- **Black frame on protected content** — exclusive-fullscreen games, DRM video, and
  some hardware-overlay surfaces are not composited by DWM and return a uniform frame.
  A perfectly uniform frame is the "nothing rendered" signal.
- **No cursor** — PrintWindow does not include the mouse cursor.
- **Hung windows** — PrintWindow is synchronous; a window whose message loop is frozen
  can block the capture (no watchdog/timeout yet).
- **Aero Snap** — a snap-arranged window may restore to its pre-snap position after a
  minimized capture, because Windows reports the pre-snap placement. Maximized and
  always-on-top windows are preserved.
- **Per-window, not the desktop** — for whole-monitor or arbitrary-region grabs use a
  screen-capture tool (`mss`, `dxcam`); those cannot see occluded or minimized windows.
- **Development-process use** — editor / tool / dev-build windows, not
  anti-cheat-protected live games.

## Dependencies

| Package | Use | License |
|---------|-----|---------|
| `pywin32` | Win32 APIs (PrintWindow, window placement) | PSF |
| `numpy` | bitmap buffer → ndarray | BSD-3 |
| `opencv-python` | color conversion, save, analysis | Apache-2.0 |

Core runtime is **Python 3.14**. Optional OCR (planned) runs in a separate Python
3.13 venv because `paddlepaddle` has no 3.14 wheel.

## License

MIT — see [LICENSE](LICENSE).
