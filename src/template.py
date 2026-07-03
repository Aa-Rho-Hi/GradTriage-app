"""Stage 5 — deterministic descriptive summary. No LLM, no scoring.

Renders a detailed, sectioned profile of an applicant: academic standing,
research interests, factual highlights ("strengths"), and the applicant's own
stated motivation (quoted from the SOP, never interpreted). Every line is
derived from data already present; nothing is invented or judged.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .analyze import motivation_sentences


def _name(rec: dict) -> str:
    p = rec.get("personal", {})
    return p.get("full_name") or rec.get("cas_id", "The applicant")


def _best_gpa(rec: dict):
    gpas = [(e.get("college_name", "an institution"), e["gpa"])
            for e in rec.get("education", []) if "gpa" in e]
    return max(gpas, key=lambda x: x[1]["normalized_4"]) if gpas else None


def _doc_areas(rec: dict) -> List[str]:
    docs = rec.get("_documents") or {}
    areas = set()
    for key in ("sop", "resume"):
        d = docs.get(key)
        if d and d.get("analysis"):
            areas.update(d["analysis"].get("detected_areas") or [])
    return sorted(areas)


def _doc_skills(rec: dict) -> List[str]:
    docs = rec.get("_documents") or {}
    skills = set()
    for key in ("sop", "resume"):
        d = docs.get(key)
        if d and d.get("analysis"):
            skills.update(d["analysis"].get("mentioned_skills") or [])
    return sorted(skills)


def _analysis(doc: dict | None) -> dict:
    return (doc or {}).get("analysis") or {}


def _profile(doc: dict | None) -> dict:
    return _analysis(doc).get("profile") or {}


def _phr(label: str, items: List[str], limit: int = 3) -> str:
    vals = [str(x).strip().rstrip(".") for x in (items or []) if str(x).strip()]
    if not vals:
        return ""
    return f"{label}: " + "; ".join(vals[:limit]) + "."


def render(rec: Dict[str, Any]) -> str:
    name = _name(rec)
    docs = rec.get("_documents") or {}
    resume = docs.get("resume")
    resume_prof = _profile(resume)
    resume_profile_lines = [ln for vals in resume_prof.values() if isinstance(vals, list)
                            for ln in vals]
    resume_gpa_lines = [ln for ln in resume_profile_lines if "gpa" in ln.lower()]
    resume_test_lines = resume_prof.get("test_scores", [])
    paras: List[str] = []

    # ---- Overview ----
    p1 = [name]
    email = rec.get("personal", {}).get("email")
    if email:
        p1[0] += f" ({email})"
    progs = rec.get("programs", [])
    if progs:
        p1.append(f"applied to {len(progs)} program(s); primary: {progs[0].get('name', 'n/a')}.")
        if progs[0].get("status"):
            p1.append(f"Recorded status: {progs[0]['status']}.")
    paras.append(" ".join(p1))

    # ---- Academic standing ----
    a = ["Academic standing —"]
    best = _best_gpa(rec)
    edu = rec.get("education", [])
    if best:
        nm, g = best
        a.append(f"strongest transcript GPA {g['raw']} on a {g['scale']:.0f}-point scale "
                 f"(≈{g['normalized_4']}/4.0) from {nm}.")
    elif resume_gpa_lines:
        a.append("no transcript GPA in the application data, but the resume mentions: "
                 + "; ".join(resume_gpa_lines[:2]) + ".")
    else:
        a.append("no transcript GPA on file.")
    insts = [e.get("college_name") for e in edu if e.get("college_name")]
    if len(insts) > 1:
        a.append("Institutions: " + "; ".join(insts) + ".")
    eng = rec.get("english_proficiency", {})
    tests = []
    if "best_ielts_overall" in eng:
        tests.append(f"IELTS {eng['best_ielts_overall']}")
    if "best_toefl_total" in eng:
        tests.append(f"TOEFL {eng['best_toefl_total']:.0f}")
    if rec.get("gre_results"):
        gre_vals = ", ".join(str(int(v)) if float(v).is_integer() else str(v)
                             for v in rec["gre_results"])
        valid_total = any(260 <= float(v) <= 340 for v in rec["gre_results"])
        tests.append(f"GRE {gre_vals} (of 340)" if valid_total
                     else f"GRE {gre_vals} (reported; flagged — not a valid 260–340 total)")
    # Application sheet is authoritative; packet-parsed figures fill gaps, marked
    # "(parsed)" so the reviewer verifies them against official records.
    ts = ((docs.get("scores") or {}).get("analysis") or {}).get("test_scores") or {}
    parsed_used = False
    if "best_ielts_overall" not in eng and "ielts_overall" in ts:
        tests.append(f"IELTS {ts['ielts_overall']} (parsed)"); parsed_used = True
    if "best_toefl_total" not in eng and "toefl_total" in ts:
        tests.append(f"TOEFL {ts['toefl_total']} (parsed)"); parsed_used = True
    if not rec.get("gre_results") and ("gre_verbal" in ts or "gre_quant" in ts):
        g = []
        if "gre_verbal" in ts:
            g.append(f"V{ts['gre_verbal']}")
        if "gre_quant" in ts:
            g.append(f"Q{ts['gre_quant']}")
        if "gre_awa" in ts:
            g.append(f"AWA {ts['gre_awa']}")
        tot = f" ({ts['gre_total']} V+Q)" if "gre_total" in ts else ""
        tests.append("GRE " + "/".join(g) + tot + " (parsed)"); parsed_used = True
    if tests:
        a.append("Test scores: " + ", ".join(tests) + ".")
    elif resume_test_lines:
        a.append("Test scores: none in structured fields, but the resume mentions: "
                 + "; ".join(resume_test_lines[:2]) + ".")
    else:
        a.append("Test scores: none on file.")
    paras.append(" ".join(a))

    # ---- Research interests & direction ----
    interests = rec.get("interests", {})
    stated = (interests.get("specialization") or []) + (interests.get("areas") or [])
    detected = _doc_areas(rec)
    skills = _doc_skills(rec)
    ri = ["Research interests —"]
    if stated:
        ri.append("stated area(s): " + ", ".join(sorted(set(stated))) + ".")
    if detected:
        ri.append("areas evident in the resume/SOP (keyword detection): " + ", ".join(detected) + ".")
    if skills:
        ri.append("technical skills/tools mentioned: " + ", ".join(skills) + ".")
    if len(ri) == 1:
        ri.append("no clear research area found in the available materials.")
    paras.append(" ".join(ri))

    # ---- Application materials detail ----
    if resume:
        rp = resume_prof
        bits = [
            _phr("education/training", rp.get("education", []), 3),
            _phr("technical preparation", rp.get("technical_preparation", []), 3),
            _phr("experience", rp.get("experience", []), 5),
            _phr("projects or applied work", rp.get("projects", []), 4),
            _phr("distinctions", rp.get("distinctions", []), 3),
        ]
        body = " ".join(b for b in bits if b)
        if body:
            paras.append("Resume detail — " + body)
        else:
            paras.append("Resume detail — resume text was captured, but no confident "
                         "education, experience, project, or skill lines were extracted.")

    sop = docs.get("sop")
    if sop and sop.get("text"):
        sp = _profile(sop)
        # NB: stated goals are quoted in the dedicated "Motivation" paragraph
        # below, so they are intentionally omitted here to avoid repeating the
        # same sentences twice per applicant.
        bits = [
            _phr("preparation described", sp.get("preparation", []), 3),
            _phr("program fit mentioned", sp.get("program_fit", []), 2),
            _phr("career direction", sp.get("career_direction", []), 2),
        ]
        body = " ".join(b for b in bits if b)
        if body:
            paras.append("SOP detail — " + body)
        else:
            paras.append("SOP detail — SOP text was captured, but the parser did not find "
                         "clear goal, preparation, or program-fit sentences.")

    lors_all = docs.get("lors") or []
    likert = next((l.get("likert") for l in lors_all if l.get("likert")), None)
    evaluators = (likert or {}).get("evaluators") or []
    # roster = every named recommender (Likert reviewers ∪ letter-only writers)
    roster = next((l.get("recommenders") for l in lors_all if l.get("recommenders")), None)
    if not roster:
        roster = [e.get("evaluator") for e in evaluators if e.get("evaluator")]
    if not roster:
        roster = [l.get("recommender") for l in lors_all if l.get("recommender")]
    n_lor = len(roster) if roster else len(lors_all)
    # narrative excerpts pulled from the actual letters (across all LOR pages)
    evidence: List[str] = []
    for lor in lors_all:
        for e in _profile(lor).get("evidence", []):
            if e not in evidence:
                evidence.append(e)
    if roster or lors_all:
        if roster:
            line = (f"Recommendation detail — {n_lor} reference letter(s) "
                    f"from {', '.join(roster)}.")
            if evaluators and len(roster) > len(evaluators):
                line += (f" {len(evaluators)} submitted the CAS rating form; "
                         f"the remainder wrote a letter only.")
        else:
            line = (f"Recommendation detail — {len(lors_all)} letter(s) on file, "
                    f"but recommender names were not parsed.")
        if evidence:
            line += (" Excerpts from the letters: "
                     + " ".join(f"“{e.rstrip('.')}.”" for e in evidence[:4]))
        paras.append(line)

    # ---- Highlights ("strengths") — factual, threshold-based, not a verdict ----
    hi: List[str] = []
    if best and best[1]["normalized_4"] >= 3.7:
        hi.append(f"excellent GPA (≈{best[1]['normalized_4']}/4.0)")
    elif best and best[1]["normalized_4"] >= 3.3:
        hi.append(f"strong GPA (≈{best[1]['normalized_4']}/4.0)")
    if eng.get("best_ielts_overall", 0) >= 7 or eng.get("best_toefl_total", 0) >= 100:
        hi.append("English score at/above the typical bar")
    if len(detected) >= 2:
        hi.append(f"document text touches {len(detected)} research-area keyword groups")
    if len(skills) >= 4:
        hi.append(f"a broad technical toolset ({len(skills)} tools cited)")
    if resume:
        hi.append("a resume on file detailing experience")
    if n_lor:
        hi.append(f"{n_lor} letter(s) of recommendation")
    if hi:
        paras.append("Highlights from the data: " + "; ".join(hi) + ".")

    review_notes = []
    if resume and sop and lors_all:
        review_notes.append("core narrative materials are present: resume, SOP, and at least one LOR.")
    else:
        missing = []
        if not resume:
            missing.append("resume")
        if not sop:
            missing.append("SOP")
        if not lors_all:
            missing.append("LOR")
        if missing:
            review_notes.append(" and ".join(missing) + " material is missing or was not identified.")
    if not best and not resume_gpa_lines:
        review_notes.append("academic performance cannot be evaluated from GPA in the parsed data.")
    if not tests and not resume_test_lines:
        review_notes.append("standardized-test evidence is absent from parsed fields.")
    if review_notes:
        paras.append("Review notes — " + " ".join(review_notes))

    # ---- Motivation (in the applicant's own words; quoted, not interpreted) ----
    if sop and sop.get("text"):
        quotes = motivation_sentences(sop["text"])
        if quotes:
            body = " ".join(f"“{q}”" for q in quotes)
            paras.append("Motivation, in their own words (quoted from the SOP, not interpreted): " + body)
        else:
            paras.append("Motivation: an SOP is on file, but no explicit goal/motivation "
                         "statements were detected to quote.")
    else:
        paras.append("Motivation: no statement of purpose on file to draw from.")

    # ---- Reference (LOR) ratings — CAS 1–5 Likert, averaged ----
    lors_all = docs.get("lors") or []
    likert = next((l.get("likert") for l in lors_all if l.get("likert")), None)
    if likert and likert.get("count"):
        evs = likert["evaluators"]
        per = "; ".join(
            f"{e.get('evaluator') or 'evaluator ' + str(i + 1)} {e['average']}/5"
            for i, e in enumerate(evs))
        line = (f"Reference ratings (CAS 1–5 Likert: Average=1 … Exceptional=5) — "
                f"{likert['count']} evaluator(s): {per}.")
        if likert.get("overall_average") is not None:
            line += f" Overall average {likert['overall_average']}/5."
        recs = [e.get("recommendation") for e in evs if e.get("recommendation")]
        if recs:
            uniq = sorted(set(recs))
            # capitalize each distinct phrase (e.g. "I highly recommend" / "I recommend")
            shown = [p[:1].upper() + p[1:] for p in uniq]
            if len(uniq) == 1:
                allword = "all " if len(recs) > 1 else ""
                line += f" Recommendation: {allword}“{shown[0]}.”"
            else:
                line += " Recommendations vary: " + "; ".join(f"“{s}”" for s in shown) + "."
        paras.append(line)

    # ---- Documents on file ----
    scores_doc = docs.get("scores")
    doc_bits = []
    if resume:
        doc_bits.append(f"resume ({resume.get('word_count', 0)} words)")
    if sop:
        doc_bits.append(f"SOP ({sop.get('word_count', 0)} words)")
    lors = docs.get("lors") or []
    if roster:                           # union roster = true recommender count
        doc_bits.append(f"{n_lor} LOR(s) from " + ", ".join(roster))
    elif lors:
        doc_bits.append(f"{len(lors)} LOR(s)")
    if scores_doc:
        doc_bits.append(f"score report/transcript ({scores_doc.get('word_count', 0)} words)")
    if doc_bits:
        paras.append("Documents on file: " + ", ".join(doc_bits) + ".")

    ocr = rec.get("_ocr")
    if ocr and ocr.get("ocr_pages_read"):
        paras.append(f"Note: this packet was partly scanned — {ocr['ocr_pages_read']} "
                     f"page(s) were read with OCR (text may contain minor recognition errors).")

    # ---- Data to verify ----
    warns = list(rec.get("meta", {}).get("warnings") or [])
    if parsed_used:
        warns.append("Some test scores were parsed from the packet — verify against "
                     "official records.")
    if warns:
        paras.append("Data to verify: " + " ".join(warns))

    return "\n\n".join(paras)
