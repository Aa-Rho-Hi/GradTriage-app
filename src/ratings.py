"""Read the CAS "REFERENCE RATINGS" Likert tables from a recommendation packet.

These evaluator forms rate an applicant on a 1–5 scale (Average=1, Good=2,
Very Good=3, Outstanding=4, Exceptional=5, plus an unscored "Not Observed"). In
the PDF the rating is a single check-glyph placed in one column — and plain text
extraction throws away *which* column it sits in. So this module reads the page
with character positions (pypdfium2), finds the column x-anchors from the "(1)"…
"(5)" markers, and maps each check-glyph's x to the column it falls under.

Fully local and deterministic: it reads positions, it does not guess. Works on
the digital CAS forms (vector text); a scanned/image evaluation has no glyph
positions to read and is simply skipped.
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional

# The eight criteria, in the order they appear top-to-bottom on the form.
CRITERIA = [
    "Knowledge in Chosen Field",
    "Motivation and Perseverance toward Goals",
    "Ability to Work Independently",
    "Ability to Work on a Team and Respect for Others",
    "Written Communication",
    "Oral Communication",
    "Ability/Potential to Plan and Conduct Research",
    "Ability/Potential for Leadership and Developing Others",
]
RATING_LABELS = {1: "Average", 2: "Good", 3: "Very Good", 4: "Outstanding", 5: "Exceptional"}


def _read_bytes(source) -> bytes:
    if hasattr(source, "read"):
        data = source.read()
        try:
            source.seek(0)
        except Exception:
            pass
        return data
    with open(source, "rb") as f:
        return f.read()


def _page_chars(textpage) -> List[tuple]:
    """Return [(top_y, left_x, char), ...] for every glyph on the page."""
    out = []
    n = textpage.count_chars()
    for i in range(n):
        c = textpage.get_text_range(i, 1)
        if not c.strip():
            continue
        box = textpage.get_charbox(i)        # (left, bottom, right, top)
        out.append((box[3], box[0], c))
    return out


def _rows(chars: List[tuple], tol: int = 3) -> Dict[int, List[tuple]]:
    """Group glyphs into rows by top-y, clustering ys within `tol` points so a
    glyph rendered a point or two off the row baseline (common for check marks
    in a different font) still lands in the same row."""
    rows: Dict[int, List[tuple]] = {}
    keys: List[int] = []
    for y, x, c in sorted(chars, key=lambda t: t[0]):
        for k in keys:
            if abs(k - y) <= tol:
                rows[k].append((x, c))
                break
        else:
            k = round(y)
            keys.append(k)
            rows[k] = [(x, c)]
    return rows


def _column_anchors(rows: Dict[int, List[tuple]]):
    """Find the rating-column x-anchors from the header row carrying the digit
    markers "(1) … (5)" left-to-right, evenly spaced. Returns (anchors, header_y)
    or (None, None). The even-spacing test rejects false matches such as a phone
    number or date that merely happens to contain the digits 1–5."""
    for y in sorted(rows, reverse=True):
        seq = sorted(rows[y])
        found: Dict[int, float] = {}
        for x, c in seq:
            if c in "12345" and int(c) not in found:
                found[int(c)] = x
        if not all(r in found for r in (1, 2, 3, 4, 5)):
            continue
        if not found[1] < found[2] < found[3] < found[4] < found[5]:
            continue
        gaps = [found[2] - found[1], found[3] - found[2],
                found[4] - found[3], found[5] - found[4]]
        if max(gaps) > 2.4 * min(gaps):          # irregular spacing -> not a header
            continue
        return {r: found[r] for r in (1, 2, 3, 4, 5)}, y
    return None, None


def _nearest_rating(x: float, anchors: Dict[int, float]) -> int:
    return min(anchors, key=lambda r: abs(anchors[r] - x))


def _looks_like_name(s: str) -> bool:
    parts = s.split()
    if not (2 <= len(parts) <= 4):
        return False
    return all(p[:1].isupper() for p in parts if p[:1].isalpha())


def _evaluator_name(page_text: str) -> Optional[str]:
    """The name line below an 'EVALUATOR INFORMATION' / 'EVALUATOR' marker, read
    from the properly-spaced page text."""
    lines = [ln.strip() for ln in page_text.splitlines()]
    for i, ln in enumerate(lines):
        if "EVALUATOR" in ln.upper():
            for cand in lines[i + 1:i + 4]:
                if cand and ":" not in cand and _looks_like_name(cand):
                    return cand
    return None


def _recommendation(page_text: str) -> Optional[str]:
    low = page_text.lower()
    for phrase in ("i highly recommend", "i strongly recommend",
                   "i recommend with reservation", "i do not recommend",
                   "i recommend"):
        if phrase in low:
            return phrase
    return None


def extract_page_ratings(textpage) -> Optional[Dict[str, Any]]:
    """Extract one evaluator's ratings from a page, or None if it has no table."""
    chars = _page_chars(textpage)
    if not chars:
        return None
    rows = _rows(chars)
    anchors, header_y = _column_anchors(rows)
    if anchors is None:
        return None

    # A rating is a single isolated check-glyph in the rating band (right of the
    # criterion text). A criterion-label row has nothing in the band; the wrapped
    # header-label row has many glyphs there — both are correctly excluded by the
    # "exactly one glyph" test. Collected top-to-bottom = criterion order.
    band_left = anchors[1] - 40
    marks: List[float] = []
    for y in sorted((yy for yy in rows if yy < header_y), reverse=True):
        rowtext = "".join(c for x, c in sorted(rows[y])).lower()
        if "recommend" in rowtext or "applicant id" in rowtext:
            break
        band = [x for x, c in rows[y] if x >= band_left]
        if len(band) == 1:
            marks.append(band[0])
        if len(marks) >= len(CRITERIA):
            break

    scored = [_nearest_rating(mx, anchors) for mx in marks]
    if not scored:
        return None
    if len(scored) == len(CRITERIA):
        ratings: Dict[str, Any] = dict(zip(CRITERIA, scored))
    else:
        ratings = {"_values": scored}            # couldn't align names 1:1

    page_text = textpage.get_text_range()        # properly spaced text
    return {
        "evaluator": _evaluator_name(page_text),
        "ratings": ratings,
        "average": round(statistics.mean(scored), 2),
        "scored_count": len(scored),
        "recommendation": _recommendation(page_text),
    }


def extract_likert_ratings(source) -> Dict[str, Any]:
    """Scan a packet for evaluator rating tables. Returns
    {evaluators: [...], overall_average: float|None, count: int}."""
    import pypdfium2 as pdfium
    data = _read_bytes(source)
    out: List[Dict[str, Any]] = []
    pdf = pdfium.PdfDocument(data)
    try:
        for i in range(len(pdf)):
            try:
                tp = pdf[i].get_textpage()
                r = extract_page_ratings(tp)
            except Exception:
                r = None
            if r:
                r["page"] = i + 1
                out.append(r)
    finally:
        pdf.close()
    averages = [e["average"] for e in out if e.get("average") is not None]
    return {
        "evaluators": out,
        "overall_average": round(statistics.mean(averages), 2) if averages else None,
        "count": len(out),
    }
