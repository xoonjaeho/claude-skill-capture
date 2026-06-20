# Security & Corporate Readiness

This skill is built for use inside a development organization (e.g. a game studio's
internal tooling). This page covers what it does and does not do with respect to
licensing, network access, and data.

## Dependencies and licenses

The **core** runtime dependencies are permissive and compatible with the MIT-licensed
skill. (Optional OCR pulls its own dependencies into a separate venv from
`requirements-ocr.txt` -- `paddleocr`/`paddlepaddle` and their transitive deps, both
Apache-2.0; audit them separately with `pip-licenses` in `.venv-ocr` if required.)

| Package | License | Notes |
|---------|---------|-------|
| `pywin32` | PSF | Python Software Foundation license |
| `numpy` | BSD-3-Clause | |
| `opencv-python` | Apache-2.0 | the prebuilt wheel bundles FFmpeg (LGPL-2.1); that matters only if you **redistribute the binary**, not when you `pip install` and use it |

No strong-copyleft (GPL) dependency; one weak-copyleft component (FFmpeg, LGPL-2.1) is
bundled in the opencv wheel -- see the note above, it only matters if you redistribute
the binary. Verify in your environment with `pip-licenses` (not a runtime dependency)
or `pip show <pkg>`.

## Network and telemetry

- **The core makes no network access.** It opens no outbound connections, sends no
  telemetry, and phones nothing home -- fully usable on an air-gapped machine. (OCR is
  optional and its first run downloads models unless you vendor them offline; see
  *OCR models* below.)
- The only files it writes are the captures you ask for (PNG/MP4 at the path you give)
  and a small recovery ledger (see below).

## Data handling

- Captured frames are written as local files where `--out`/`--record` point. Nothing
  is uploaded.
- The crash-recovery ledger at `%LOCALAPPDATA%\capture\parked.json` stores window
  handles, placement and style flags, the target window's class and process identity,
  and the owning capture process's identity -- **no window content (no pixels)**.
- Captures can contain whatever is on screen. Treat the output files with the same
  sensitivity as a screenshot.

## Sensitive windows

A `--title` that matches more than one window is **refused**, not silently resolved,
so an ambiguous match can't grab a sensitive window you didn't mean. Disambiguate with
`--hwnd` (authoritative) or `--exact` (exact-title match). This is enforced on every
resolution path (single capture, `--wait`, and `--loop` streaming).

## Supply-chain pinning (optional)

For locked, reproducible installs, generate a hash-pinned requirements file on the
target platform and install with `--require-hashes`:

```
pip download -r requirements.txt -d wheels/        # fetch the exact wheels
pip hash wheels/*                                  # print --hash lines
# or, with pip-tools:
pip-compile --generate-hashes requirements.txt -o requirements-lock.txt
pip install --require-hashes -r requirements-lock.txt
```

Hashes are platform- and Python-version-specific (Windows / CPython 3.14 here), so
generate them in the environment you deploy to.

## OCR models

OCR (optional, a separate Python 3.13 subprocess) uses PaddleOCR. The OCR venv brings
its own dependencies (`paddleocr`, `paddlepaddle`, and their transitive deps) -- audit
them separately with `pip-licenses` in `.venv-ocr` if your policy requires it; they are
isolated from the core. The first OCR run downloads the PP-OCR models to
`%USERPROFILE%\.paddlex`. For air-gapped or corporate deployment, **vendor the models
offline** -- download them once on a connected machine and copy the `.paddlex` model
cache to the target, so the worker never fetches anything at runtime.

## Intended scope

Development-process windows -- editors, tools, dev builds. Not anti-cheat-protected
live games (PrintWindow returns black under exclusive fullscreen, and window
manipulation can trip anti-cheat).
