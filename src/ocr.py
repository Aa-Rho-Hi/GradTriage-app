"""Local OCR fallback for scanned (image-only) PDF pages.

Uses pypdfium2 to render a page to an image, lightly cleans the image
(grayscale → autocontrast → Otsu binarization → small-angle deskew), then reads
it with Tesseract (via pytesseract, LSTM engine). Fully offline — no network, no
LLM. OCR is OPTIONAL: if the tools aren't installed, the pipeline still runs and
flags the scanned pages instead of crashing.

The preprocessing + 300-DPI rasterization meaningfully cut recognition errors vs.
feeding Tesseract the raw render, and every step degrades gracefully (any failure
falls back to the previous image), so OCR never raises.

Install (macOS):  brew install tesseract            # Tesseract 5.x recommended
                  pip install pypdfium2 pytesseract pillow
Install (Ubuntu): apt-get install tesseract-ocr
"""
from __future__ import annotations

import io
import os
import re
from functools import lru_cache
from typing import Any, Dict, List, Tuple

# Ligatures and a few safe OCR substitutions applied ONLY to OCR'd text.
_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
              "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}


def clean_ocr_text(text: str) -> str:
    """Fix common OCR recognition artifacts. Conservative — only changes that are
    safe on prose: ligatures, and a standalone vertical bar that Tesseract emits
    for a capital 'I' (e.g. '| have known' → 'I have known'). Applied to OCR'd
    pages only; digital text is never altered."""
    if not text:
        return text
    for k, v in _LIGATURES.items():
        text = text.replace(k, v)
    # a lone "|" (surrounded by whitespace/edges) is a misread capital "I"
    text = re.sub(r"(?<!\S)\|(?!\S)", "I", text)
    # "|" or "l" glued to the front of a capitalised word at a sentence start,
    # e.g. "|t was", "|n my" -> "It", "In"
    text = re.sub(r"(?<!\S)\|(?=[a-z]{1,3}\b)", "I", text)
    text = re.sub(r"[ \t]{2,}", " ", text)             # collapse runs of spaces
    return text

# A page is treated as "scanned" (and OCR'd) when its embedded text is sparse.
# A bare character threshold is not enough: scanned pages routinely carry a small
# amount of embedded text — a "Scanned by CamScanner" watermark, a page number, a
# header — that sits above a naive threshold while the real content is an image.
# So we trigger OCR on a low *word* count, and on a slightly higher word count
# when the page also contains a raster image (i.e. a scan with a caption/stamp).
MIN_WORDS_ALWAYS_OCR = 18       # near-empty page -> always OCR
MIN_WORDS_IMAGE_PAGE = 70       # page with an image and little text -> scanned, OCR it
MAX_OCR_PAGES = 80              # bound OCR work per document (cost control)
OCR_PAGE_TIMEOUT = 120         # seconds per page — guard against pathological scans
OCR_MAX_WORKERS = 8            # parallel OCR worker processes (pages are the unit)
RENDER_DPI = 300               # rasterization DPI for scanned pages
RENDER_SCALE = RENDER_DPI / 72.0   # pypdfium renders at 72 dpi per unit scale
TESS_CONFIG = "--oem 1 --psm 3"    # LSTM engine, automatic page segmentation
DESKEW_MAX_DEG = 5.0           # only correct small scan skews
DESKEW_STEP_DEG = 0.5
_DESKEW_EST_WIDTH = 800        # downscale width used to estimate the skew angle


@lru_cache(maxsize=1)
def available() -> bool:
    """True if OCR can actually run (libraries + tesseract binary present)."""
    try:
        import importlib.util
        if importlib.util.find_spec("pypdfium2") is None:
            return False
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _estimate_skew(gray) -> float:
    """Estimate small page skew (degrees) via a projection-profile search on a
    downscaled copy. The angle whose horizontal projection has the sharpest
    row-to-row variation best aligns the text lines. Deterministic; CPU-cheap."""
    import numpy as np
    from PIL import Image, ImageOps

    w, h = gray.size
    if w > _DESKEW_EST_WIDTH:
        gray = gray.resize((_DESKEW_EST_WIDTH,
                            max(1, int(h * _DESKEW_EST_WIDTH / w))), Image.BILINEAR)
    inv = ImageOps.invert(gray)        # text bright on dark, so row sums peak on text
    best_angle, best_score = 0.0, -1.0
    steps = int(DESKEW_MAX_DEG / DESKEW_STEP_DEG)
    for k in range(-steps, steps + 1):
        ang = k * DESKEW_STEP_DEG
        rot = inv.rotate(ang, resample=Image.BILINEAR, fillcolor=0)
        proj = np.asarray(rot, dtype=np.float32).sum(axis=1)
        score = float(np.square(np.diff(proj)).sum())
        if score > best_score:
            best_score, best_angle = score, ang
    return best_angle


def _otsu_binarize(gray):
    """Binarize a grayscale image at Otsu's threshold (numpy). Returns mode-'L'."""
    import numpy as np
    from PIL import Image

    arr = np.asarray(gray, dtype=np.uint8)
    hist = np.bincount(arr.ravel(), minlength=256).astype(np.float64)
    total = arr.size
    sum_total = float(np.dot(np.arange(256), hist))
    sumB = wB = 0.0
    maximum, threshold = 0.0, 127
    for t in range(256):
        wB += hist[t]
        if wB == 0:
            continue
        wF = total - wB
        if wF == 0:
            break
        sumB += t * hist[t]
        mB = sumB / wB
        mF = (sum_total - sumB) / wF
        between = wB * wF * (mB - mF) ** 2
        if between > maximum:
            maximum, threshold = between, t
    binary = (arr > threshold).astype(np.uint8) * 255
    return Image.fromarray(binary, mode="L")


def _preprocess(pil):
    """Clean a rendered page for OCR. Each step falls back on failure so OCR
    always gets *some* image and never raises here."""
    from PIL import Image, ImageOps

    try:
        gray = pil.convert("L")
    except Exception:
        return pil
    try:
        gray = ImageOps.autocontrast(gray)
    except Exception:
        pass
    try:
        angle = _estimate_skew(gray)
        if abs(angle) >= DESKEW_STEP_DEG:
            gray = gray.rotate(angle, resample=Image.BILINEAR,
                               fillcolor=255, expand=False)
    except Exception:
        pass
    try:
        return _otsu_binarize(gray)
    except Exception:
        return gray


def _tesseract_text(img) -> str:
    import pytesseract
    try:
        return pytesseract.image_to_string(
            img, lang="eng", config=TESS_CONFIG, timeout=OCR_PAGE_TIMEOUT) or ""
    except Exception:                         # timeout / transient error
        return ""


@lru_cache(maxsize=1)
def _rapidocr_engine():
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR()


def _rapidocr_text(img) -> str:
    """Optional higher-accuracy local engine (ONNX, offline). Falls back to
    Tesseract if RapidOCR isn't installed or errors."""
    try:
        import numpy as np
        result, _ = _rapidocr_engine()(np.asarray(img.convert("RGB")))
        return "\n".join(line[1] for line in result) if result else ""
    except Exception:
        return _tesseract_text(img)


def _engine_text(img) -> str:
    """Run the configured OCR engine. GRADAPP_OCR_ENGINE=rapidocr selects the
    stronger local engine for scanned documents; default is Tesseract."""
    if os.environ.get("GRADAPP_OCR_ENGINE", "tesseract").strip().lower() == "rapidocr":
        return _rapidocr_text(img)
    return _tesseract_text(img)


def _ocr_one(args) -> Tuple[int, str]:
    """Render + preprocess + OCR a single page. Module-level so it can run in a
    worker process. Opens the PDF by path so the bytes aren't copied per task."""
    path, index = args
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(path)
    try:
        if not (0 <= index < len(pdf)):
            return index, ""
        pil = pdf[index].render(scale=RENDER_SCALE).to_pil()
        img = _preprocess(pil)
        return index, clean_ocr_text(_engine_text(img).strip())
    finally:
        pdf.close()


def _ocr_pages(data: bytes, indices: List[int]) -> Dict[int, str]:
    """OCR the given page indices, in parallel across processes (Tesseract is
    single-threaded per call, so pages are the unit of parallelism). Falls back to
    serial if a process pool can't start (restricted environments)."""
    import tempfile
    if not indices:
        return {}
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tf.write(data)
        tmp = tf.name
    out: Dict[int, str] = {}
    try:
        tasks = [(tmp, i) for i in indices]
        workers = min(OCR_MAX_WORKERS, len(tasks), os.cpu_count() or 1)
        if workers <= 1:
            for t in tasks:
                idx, txt = _ocr_one(t)
                out[idx] = txt
        else:
            try:
                from concurrent.futures import ProcessPoolExecutor
                with ProcessPoolExecutor(max_workers=workers) as ex:
                    for idx, txt in ex.map(_ocr_one, tasks):
                        out[idx] = txt
            except Exception:
                for t in tasks:                # pool unavailable -> serial
                    try:
                        idx, txt = _ocr_one(t)
                        out[idx] = txt
                    except Exception:
                        out[t[1]] = ""
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return out


def _page_has_image(page) -> bool:
    """Fast check (no image decode) for a raster image on the page, by inspecting
    the resource dictionary's XObjects. Used to spot scanned pages whose only
    embedded text is a watermark/header."""
    try:
        res = page.get("/Resources")
        if res is None:
            return False
        xobjects = res.get_object().get("/XObject")
        if xobjects is None:
            return False
        xobjects = xobjects.get_object()
        for key in xobjects:
            obj = xobjects[key].get_object()
            if obj.get("/Subtype") == "/Image":
                return True
    except Exception:
        return False
    return False


def _needs_ocr(text: str, has_image: bool) -> bool:
    words = len(text.split())
    if words < MIN_WORDS_ALWAYS_OCR:
        return True
    if has_image and words < MIN_WORDS_IMAGE_PAGE:
        return True
    return False


def pdf_page_texts(data: bytes) -> Tuple[List[str], Dict[str, Any]]:
    """Return (per-page text, info). Pages whose embedded text is too sparse to be
    a real text page are OCR'd when OCR is available; the longer of the embedded
    vs OCR'd text is kept, so a good text page can never be made worse. `info`
    reports how many pages looked scanned and how many were actually OCR'd."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    pages = [(p.extract_text() or "") for p in reader.pages]
    scanned = [i for i, page in enumerate(reader.pages)
               if _needs_ocr(pages[i], _page_has_image(page))]
    info = {"scanned_pages": len(scanned), "ocr_used": 0, "ocr_unavailable": False}
    if scanned:
        if available():
            todo = scanned[:MAX_OCR_PAGES]
            for i, t in _ocr_pages(data, todo).items():
                # keep whichever is longer: never lose good embedded text, but
                # fill in (or replace a bare watermark on) a scanned page.
                if len(t.strip()) > len(pages[i].strip()):
                    pages[i] = t
                    info["ocr_used"] += 1
            if len(scanned) > MAX_OCR_PAGES:
                info["ocr_capped"] = True
        else:
            info["ocr_unavailable"] = True
    return pages, info
