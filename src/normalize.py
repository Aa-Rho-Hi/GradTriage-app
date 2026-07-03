"""Stage 4 — deterministic normalization.

Turns a parsed row into a canonical student record (dict). Handles type
coercion, GPA scale normalization, and assembling indexed/scalar columns into
arrays. Works across export layouts via candidate column-name lists.

Protected attributes (race, gender, age, citizenship, phone) are deliberately
NOT mapped — they must never feed an admissions evaluation.
No LLM is involved here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import SCHEMA_VERSION
from .parse import group_indexed

# Candidate column bases per concept (first one with data wins). This is what
# makes the parser tolerant of different exports.
IELTS_BASES = ["ielts_official_overall_band_score", "ielts_overall_band_score"]
TOEFL_TOTAL_BASES = ["toefl_ibt_official_score", "toefl_ibt_result"]
TOEFL_SECTIONS = {
    "listening": ["toefl_ibt_listening"],
    "reading": ["toefl_ibt_reading"],
    "speaking": ["toefl_ibt_speaking"],
    "writing": ["toefl_ibt_writing"],
}
GRE_BASES = ["gre_general_official_overall_result"]
COLLEGE_NAME_BASES = ["transcript_college", "college_name"]
COLLEGE_COUNTRY_BASES = ["country_name_of_college"]
COLLEGE_STATE_BASES = ["college_state"]
GPA_BASE = "gpas_by_transcript_gpa"
DESIGNATION_BASES = ["designation"]
LABEL_BASES = ["designation_label"]
STATUS_BASES = ["local_status", "application_status"]
DEPARTMENT_BASES = ["designation_department_name"]
LEVEL_BASES = ["designation_program_level"]
TERM_BASES = ["designation_program_start_term"]
YEAR_BASES = ["designation_program_start_year"]


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    v = str(v).strip().replace("%", "")
    if v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _series(scalars: Dict[str, str], groups: Dict[str, Dict[int, str]],
            base: str) -> Dict[int, str]:
    """Return {index -> value} for a base, whether it's indexed or scalar."""
    if base in groups:
        return groups[base]
    val = scalars.get(base, "")
    return {0: val} if val != "" else {}


def _first_series(scalars, groups, bases: List[str]) -> Dict[int, str]:
    for b in bases:
        s = _series(scalars, groups, b)
        if s:
            return s
    return {}


def _detect_scale(value: float, cfg: dict) -> float:
    forced = cfg.get("gpa", {}).get("gpa_scale")
    if forced:
        return float(forced)
    for band in cfg.get("gpa", {}).get("auto_detect_thresholds", []):
        if value <= band["max"]:
            return float(band["scale"])
    return 100.0


def _normalize_gpa(raw: float, cfg: dict) -> Dict[str, float]:
    scale = _detect_scale(raw, cfg)
    norm = round(min(raw / scale * 4.0, 4.0), 2)
    return {"raw": raw, "scale": scale, "normalized_4": norm}


def build_record(row: Dict[str, str], cfg: dict, source_file: str,
                 source_row: int) -> Dict[str, Any]:
    scalars, groups = group_indexed(row)
    warnings: List[str] = []

    first = scalars.get("first_name", "").strip()
    last = scalars.get("last_name", "").strip()
    email = scalars.get("email", "").strip()

    # identifier: cas_id -> email -> generated. cas_id is the cross-document
    # merge key, so flag when it is missing.
    has_cas = bool(scalars.get("cas_id", "").strip())
    cas_id = scalars.get("cas_id", "").strip() or email or f"ROW-{source_row}"
    if not has_cas and email:
        warnings.append("No cas_id column; keyed by email — add cas_id for reliable "
                        "cross-document merging (transcripts, SOPs, LORs).")
    elif not has_cas and not email:
        warnings.append("No cas_id or email; generated a row-based id (cannot merge "
                        "other documents to this student).")

    rec: Dict[str, Any] = {"schema_version": SCHEMA_VERSION, "cas_id": cas_id}

    # ---- personal ----
    personal = {}
    if first:
        personal["first_name"] = first
    if last:
        personal["last_name"] = last
    if first or last:
        personal["full_name"] = " ".join(p for p in [first, last] if p)
    if email:
        personal["email"] = email
    if personal:
        rec["personal"] = personal

    # ---- english proficiency ----
    eng: Dict[str, Any] = {}
    ielts_series = _first_series(scalars, groups, IELTS_BASES)
    ielts = []
    for i in sorted(ielts_series):
        band = _to_float(ielts_series[i])
        if band is not None:
            ielts.append({"attempt": i, "overall_band": band})
    if ielts:
        eng["ielts"] = ielts
        eng["best_ielts_overall"] = max(a["overall_band"] for a in ielts)

    total_series = _first_series(scalars, groups, TOEFL_TOTAL_BASES)
    section_series = {k: _first_series(scalars, groups, b) for k, b in TOEFL_SECTIONS.items()}
    toefl = []
    idx_union = set(total_series) | {i for s in section_series.values() for i in s}
    for i in sorted(idx_union):
        attempt: Dict[str, Any] = {"attempt": i}
        tot = _to_float(total_series.get(i, ""))
        if tot is not None:
            attempt["total"] = tot
        for key, s in section_series.items():
            val = _to_float(s.get(i, ""))
            if val is not None:
                attempt[key] = val
        if len(attempt) > 1:
            toefl.append(attempt)
    if toefl:
        eng["toefl"] = toefl
        totals = [a["total"] for a in toefl if "total" in a]
        if totals:
            eng["best_toefl_total"] = max(totals)
    if eng:
        rec["english_proficiency"] = eng

    # ---- GRE (stored loosely, not scored) ----
    gre_series = _first_series(scalars, groups, GRE_BASES)
    gre_results = [v for v in (_to_float(gre_series[i]) for i in sorted(gre_series))
                   if v is not None]
    if gre_results:
        rec["gre_results"] = gre_results
        if not any(260 <= v <= 340 for v in gre_results):
            warnings.append("GRE value(s) present but none are a valid 0-340 total "
                            "(looks like section scores); not scored.")

    # ---- education (college name + gpa, paired by index) ----
    name_series = _first_series(scalars, groups, COLLEGE_NAME_BASES)
    country_series = _first_series(scalars, groups, COLLEGE_COUNTRY_BASES)
    state_series = _first_series(scalars, groups, COLLEGE_STATE_BASES)
    gpa_series = _series(scalars, groups, GPA_BASE)
    education = []
    gpa_scale_inferred = False
    for i in sorted(set(name_series) | set(gpa_series) | set(country_series) | set(state_series)):
        entry: Dict[str, Any] = {"index": i}
        if name_series.get(i, "").strip():
            entry["college_name"] = name_series[i].strip()
        if country_series.get(i, "").strip():
            entry["country"] = country_series[i].strip()
        if state_series.get(i, "").strip():
            entry["state"] = state_series[i].strip()
        gpa_raw = _to_float(gpa_series.get(i, ""))
        if gpa_raw is not None:
            entry["gpa"] = _normalize_gpa(gpa_raw, cfg)
            gpa_scale_inferred = not cfg.get("gpa", {}).get("gpa_scale")
        if len(entry) > 1:
            education.append(entry)
    if education:
        rec["education"] = education
        if any("gpa" in e for e in education) and gpa_scale_inferred:
            warnings.append("GPA scale was auto-detected from the value (e.g. 3.8 is "
                            "read as /4.0 even if the transcript uses /5.0) — verify, "
                            "or set gpa.gpa_scale in config/csv_field_map.yaml.")

    # ---- programs applied to (designation_* and friends) ----
    desig_series = _first_series(scalars, groups, DESIGNATION_BASES)
    label_series = _first_series(scalars, groups, LABEL_BASES)
    status_series = _first_series(scalars, groups, STATUS_BASES)
    dept_series = _first_series(scalars, groups, DEPARTMENT_BASES)
    level_series = _first_series(scalars, groups, LEVEL_BASES)
    term_series = _first_series(scalars, groups, TERM_BASES)
    year_series = _first_series(scalars, groups, YEAR_BASES)
    programs = []
    for i in sorted(set(desig_series) | set(status_series) | set(dept_series)):
        entry: Dict[str, Any] = {"index": i}
        for key, s in [("name", desig_series), ("label", label_series),
                       ("status", status_series), ("department", dept_series),
                       ("level", level_series), ("start_term", term_series),
                       ("start_year", year_series)]:
            v = s.get(i, "").strip()
            if v:
                entry[key] = v
        if len(entry) > 1:
            programs.append(entry)
    if programs:
        rec["programs"] = programs

    # ---- interests (custom_questions about area of specialization) ----
    cq = cfg.get("custom_questions", {})
    areas_match = cq.get("areas_match", "which_area")
    spec_match = cq.get("specialization_match", "what_area_of_specialization")
    areas, spec = [], []
    for col, val in scalars.items():
        if not col.startswith("custom_questions") or not str(val).strip():
            continue
        if spec_match in col:
            spec.append(val.strip())
        elif areas_match in col:
            areas.append(val.strip())
    interests: Dict[str, Any] = {}
    if areas:
        interests["areas"] = sorted(set(areas))
    if spec:
        interests["specialization"] = sorted(set(spec))
    if interests:
        rec["interests"] = interests

    # ---- warnings ----
    if "english_proficiency" not in rec:
        warnings.append("No IELTS or TOEFL score found.")
    if not any("gpa" in e for e in rec.get("education", [])):
        warnings.append("No transcript GPA found.")

    rec["meta"] = {
        "source_file": source_file,
        "source_row": source_row,
        "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "validation_status": "valid_with_warnings" if warnings else "valid",
        "warnings": warnings,
    }
    return rec
