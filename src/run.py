"""CLI orchestrator.

    python -m src.run --input data/raw/sample_applications.csv

Flow (fully deterministic; no scoring):
  CSV/Excel -> parse -> normalize -> validate -> canonical record
            -> merge into the unified per-student record (keyed by cas_id)
            -> descriptive summary built from whatever sources exist

Each student is ONE row in the SQLite store (src/store.py), keyed by cas_id,
that accumulates sources. Re-running, or running a different source file, merges
into the same record (atomically) rather than overwriting it.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict, List

import yaml

from .analyze import analyze_text
from .normalize import build_record
from .parse import read_rows
from .models import parse_record
from .template import render
from .merge import (application_section, identity_from, merge_identity,
                    summary_view, upsert_source)
from .store import Store, db_path_for

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_yaml(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _is_empty_record(rec: Dict[str, Any]) -> bool:
    """True if a parsed record carries no usable applicant data — only a
    generated id. Used to drop blank/unmapped CSV rows."""
    p = rec.get("personal") or {}
    if any(p.get(k) for k in ("full_name", "first_name", "last_name", "email")):
        return False
    for key in ("education", "english_proficiency", "gre_results", "programs",
                "interests", "experience"):
        if rec.get(key):
            return False
    return True


def run(input_csv: str, outdir: str, field_map_path: str) -> Dict[str, Any]:
    cfg = _load_yaml(field_map_path)
    rows = read_rows(input_csv)
    src = os.path.basename(input_csv)
    store = Store(db_path_for(outdir))

    valid_ids: set = set()
    quarantine: List[dict] = []

    for i, row in enumerate(rows):
        assembled = build_record(row, cfg, src, i)
        if not assembled.get("cas_id"):
            quarantine.append({"source_row": i, "errors": ["missing cas_id"], "raw": row})
            continue
        rec, errors = parse_record(assembled)         # strong typed parse + validate
        if rec is None:
            quarantine.append({"source_row": i, "cas_id": assembled.get("cas_id"),
                               "errors": errors})
            continue

        # A row with no real applicant data and only a generated ROW-n id is junk
        # (blank/trailing CSV line or an unmapped export) — quarantine it instead
        # of surfacing an empty applicant card.
        if str(rec["cas_id"]).startswith("ROW-") and _is_empty_record(rec):
            quarantine.append({"source_row": i, "cas_id": rec["cas_id"],
                               "errors": ["row has no identifiable applicant data "
                                          "(no cas_id, email, name, scores, or programs)"]})
            continue

        sid = rec["cas_id"]

        def _apply(unified, rec=rec, i=i):
            merge_identity(unified["identity"], identity_from(rec))
            upsert_source(unified, "application", application_section(rec),
                          file=src, row=i, warnings=rec.get("meta", {}).get("warnings", []))
            unified["summary"] = render(summary_view(unified))
            return unified

        store.update(sid, _apply)                     # atomic read-modify-write
        valid_ids.add(sid)

    store.replace_quarantine(quarantine)
    index = reindex(outdir)

    return {"rows": len(rows), "valid": len(valid_ids),
            "quarantined": len(quarantine), "students_total": len(index),
            "outdir": outdir}


def reindex(outdir: str) -> List[dict]:
    """Reconcile duplicates and rebuild reports/_summaries.md from the store.
    Called after any source (CSV or document) is ingested."""
    store = Store(db_path_for(outdir))
    store.reconcile()                                 # collapse same-person duplicates

    # Re-render summaries from the current template. This keeps existing records
    # current after summary logic changes, not only after a new ingest.
    for u in store.all():
        sid = u["student_id"]

        def _refresh(current):
            sources = current.get("sources") or {}
            for stype in ("resume", "sop"):
                doc = sources.get(stype)
                if doc and doc.get("text"):
                    old_flags = ((doc.get("analysis") or {}).get("flags") or [])
                    analysis = analyze_text(doc["text"], stype)
                    for flag in old_flags:
                        if flag not in analysis["flags"]:
                            analysis["flags"].append(flag)
                    doc["analysis"] = analysis
                    doc["word_count"] = analysis["word_count"]
            for lor in sources.get("lors") or []:
                if lor and lor.get("text"):
                    old_flags = ((lor.get("analysis") or {}).get("flags") or [])
                    analysis = analyze_text(lor["text"], "lor")
                    for flag in old_flags:
                        if flag not in analysis["flags"]:
                            analysis["flags"].append(flag)
                    lor["analysis"] = analysis
                    lor["word_count"] = analysis["word_count"]
            current["summary"] = render(summary_view(current))
            return current

        store.update(sid, _refresh, create=False)

    index = store.index()

    md = ["# Applicant summaries", "",
          f"_{len(index)} students (descriptive only, no scoring)._", ""]
    for e in index:
        md.append(f"## {e['name']}  ·  {e['cas_id']}")
        md.append("")
        md.append(e["summary_text"])
        md.append("")
    report_path = os.path.join(outdir, "reports", "_summaries.md")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    return index


def main() -> None:
    ap = argparse.ArgumentParser(
        description="CSV/Excel -> unified per-student records (keyed by cas_id)")
    ap.add_argument("--input", default=os.path.join(ROOT, "data", "raw",
                                                     "sample_applications.csv"))
    ap.add_argument("--outdir", default=os.path.join(ROOT, "data"))
    ap.add_argument("--field-map", default=os.path.join(ROOT, "config",
                                                        "csv_field_map.yaml"))
    args = ap.parse_args()
    print(json.dumps(run(args.input, args.outdir, args.field_map), indent=2))


if __name__ == "__main__":
    main()
