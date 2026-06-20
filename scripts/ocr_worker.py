"""OCR worker -- runs in a dedicated Python 3.13 venv (PaddleOCR has no 3.14 wheel).

Reads a PNG image from stdin, runs PaddleOCR, and writes one JSON object to stdout:
``{"results": [{"text", "confidence", "box"}, ...]}`` on success, or
``{"error": "..."}`` on failure. Invoked by the core's capture_text() over a pipe;
it is not meant to be run by hand.

PaddleOCR/Paddle log to stdout and stderr; stdout is redirected to stderr during
inference so the only thing on real stdout is the JSON result.
"""
# paddleocr lives in the 3.13 venv, not the core env that type-checks this.
# pyright: reportMissingImports=false
import argparse
import json
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lang", default="en")
    args = parser.parse_args()

    data = sys.stdin.buffer.read()

    # Redirect fd 1 to fd 2 at the OS level for the whole OCR run, so NEITHER Python
    # prints NOR Paddle's native C++/glog writes (which a Python-level rebind misses)
    # can land on the real stdout and corrupt the JSON. fd 1 is restored only to emit
    # the JSON result.
    saved_stdout_fd = os.dup(1)
    os.dup2(2, 1)
    error = None
    results: list = []
    try:
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            error = "could not decode the image"
        else:
            from paddleocr import PaddleOCR

            ocr = PaddleOCR(
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                lang=args.lang,
                # The oneDNN inference path hits a PIR conversion bug on paddle 3.3;
                # disabling it falls back to a working path.
                enable_mkldnn=False,
            )
            prediction = ocr.predict(image)
            page = prediction[0] if prediction else None
            if page is not None:
                texts = page.get("rec_texts", [])
                scores = page.get("rec_scores", [])
                polys = page.get("rec_polys", [])
                for i, text in enumerate(texts):
                    box = polys[i].tolist() if i < len(polys) and hasattr(polys[i], "tolist") else None
                    results.append(
                        {
                            "text": text,
                            "confidence": float(scores[i]) if i < len(scores) else None,
                            "box": box,
                        }
                    )
    except Exception as exc:  # any worker failure -> structured error, not a traceback
        error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            sys.stdout.flush()  # push any buffered redirected output to stderr
        except Exception:
            pass
        os.dup2(saved_stdout_fd, 1)  # restore the real stdout
        os.close(saved_stdout_fd)

    payload = {"error": error} if error else {"results": results}
    os.write(1, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
