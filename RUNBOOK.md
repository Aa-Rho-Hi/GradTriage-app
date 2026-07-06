# Grad App — Runbook

Operational guide for the **Graduate Applicant Summary Pipeline**: what the project is, how to get it, how to run it, and what every file does. For design rationale see [ARCHITECTURE.md](ARCHITECTURE.md); for a quick overview see [README.md](README.md).

---

## 1. What this project is

A **fully local, deterministic pipeline** that turns graduate-admissions exports (CSV/Excel rows plus PDF application packets) into one validated, unified JSON record per applicant, then renders a **factual descriptive summary** for reviewers.

Hard constraints baked into the design:

- **No LLM, no scoring, no ranking** — the app summarizes data; the human reviewer judges.
- **No network** — everything runs on the reviewer's machine (SQLite, local OCR).
- **Protected attributes** (race, gender, age, citizenship, phone) are never parsed, and `extra="forbid"` on every model prevents them leaking in via new columns.
- **PII never committed** — `.gitignore` excludes all of `data/`.

## 2. Technologies used

| Layer | Technology |
|---|---|
| Language | Python 3.10–3.12 |
| Validation / schema | Pydantic v2 (single source of truth for `student.schema.json`) |
| Tabular ingestion | pandas + openpyxl (CSV, `.xlsx`, `.xls`, `.xlsm`) |
| PDF text extraction | pypdf; pypdfium2 for rendering & glyph positions |
| OCR (scanned pages, optional) | Tesseract via pytesseract + Pillow/numpy preprocessing; optional RapidOCR (`GRADAPP_OCR_ENGINE=rapidocr`) |
| DOCX extraction | python-docx |
| Storage | SQLite (WAL, atomic transactions) — `data/students.db` |
| Web UI | Flask + Flask-Limiter, Jinja2 templates |
| Config | YAML (`config/*.yaml`) |
| Tests / CI | pytest + pytest-cov (80% gate), pyflakes, reportlab (test fixtures), GitHub Actions matrix on 3.10/3.11/3.12 |

## 3. Architecture so far

Two ingestion paths converge on one unified per-student record keyed by `cas_id` (see `architecture.svg` / `architecture.png` / `architecture.mermaid`):

```
CSV/Excel ──▶ parse ──▶ normalize ──▶ validate (Pydantic) ──▶ application section ─┐
                                          │ (bad rows → data/quarantine/)          ├─▶ merge.py ──▶ unified record ──▶ store.py (SQLite) ──▶ template.py summary ──▶ CLI report / Flask UI
PDF/ZIP packets ─▶ segment (packet.py, Viterbi) ─▶ extract text (+OCR) ─▶ analyze ─┘
```

- **Application path**: `parse.py → normalize.py → models.py/validate.py`. Rows that fail validation are quarantined, not silently dropped.
- **Document path**: single files via `add_document.py`, or ZIPs of combined "Full Application" PDFs via `ingest_zip.py`. `packet.py` labels each page (resume/SOP/LOR/scores/ignore) with per-page feature scores decoded jointly by Viterbi; `ocr.py` handles scanned pages; `analyze.py` does keyword/structure analysis; `ratings.py` reads Likert reference-rating tables by glyph position.
- **Merge & storage**: `merge.py` upserts each source into a per-student record (schema v3.0.0, provenance tracked); `store.py` persists it atomically in SQLite (WAL, `BEGIN IMMEDIATE`, graceful fallback on network filesystems).
- **Output**: `template.py` renders the neutral summary; `run.py` writes `data/reports/_summaries.md`; `app.py` serves the same content as a web UI.

## 4. Clone & set up

```bash
git clone <repo-url> grad-app        # substitute your remote URL once one is configured
cd grad-app

python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt -c constraints.txt  # pinned, reproducible

# Optional — OCR for scanned PDFs:
brew install tesseract               # macOS
sudo apt-get install tesseract-ocr   # Ubuntu

# Optional — dev/test tooling:
pip install -r requirements.txt -r requirements-dev.txt -c constraints.txt
```

`constraints.txt` pins the tested versions. OCR is optional: without Tesseract the pipeline still runs and flags scanned pages instead of crashing.

## 5. Run it

### CLI pipeline

```bash
python -m src.run --input data/raw/sample_applications.csv   # CSV or .xlsx/.xls/.xlsm
```

Records land in `data/students.db`; invalid rows in `data/quarantine/`; the combined report in `data/reports/_summaries.md`.

### Documents

```bash
python -m src.add_document --input "1001_sop.pdf" --type sop          # single SOP/LOR/resume
python -m src.ingest_zip   --input packets.zip                        # ZIP of combined application PDFs
```

`cas_id` is inferred from the filename prefix (`<cas_id>_...`) or passed with `--cas-id`.

### Web UI

```bash
python -m src.app
```

Then **open http://127.0.0.1:5000** in a browser. Upload a CSV/Excel (optionally with a ZIP of packets), browse per-student summaries, download all summaries, review quarantined rows and data-to-verify flags. "Clear all" resets derived records; tick "also delete raw uploads" to purge original files from disk.

Environment variables (all optional; defaults are the safe choice):

| Env var | Default | Purpose |
|---|---|---|
| `GRADAPP_HOST` / `GRADAPP_PORT` | `127.0.0.1` / `5000` | Bind address/port. Non-loopback requires `GRADAPP_ALLOW_REMOTE=1` **and** `GRADAPP_PASSWORD`. |
| `GRADAPP_PASSWORD` | unset | Gates every route behind HTTP Basic Auth. |
| `GRADAPP_SECRET_KEY` | random per process | Cookie signing key. |
| `GRADAPP_DEBUG` | off | `1` enables the Werkzeug debugger (never on a reachable host). |
| `GRADAPP_MAX_UPLOAD_MB` | `200` | Request-body size cap. |
| `GRADAPP_FORCE_SECURE_COOKIES` | off | `1` marks cookies `Secure` (behind TLS only). |
| `GRADAPP_OCR_ENGINE` | `tesseract` | `rapidocr` selects RapidOCR if installed. |

Built-in guardrails (not configurable): upload extension allow-lists + magic-byte checks, per-form CSRF tokens + same-origin checks, rate limiting, security response headers, access log at `data/audit.log`. Full detail in README §Security.

### Tests & lint

```bash
pytest -q                                                    # full suite
python -m pyflakes src                                       # lint
pytest --cov=src --cov-report=term-missing --cov-fail-under=80   # what CI runs
```

### Regenerate the JSON schema

```bash
python -m scripts.gen_schema     # rewrites student.schema.json from the Pydantic model
```

## 6. File-by-file reference

### Root

| File | Contents |
|---|---|
| `README.md` | Project overview, quick-start commands, column mapping, security guardrails. |
| `RUNBOOK.md` | This document. |
| `ARCHITECTURE.md` | Full design doc: principles, both ingestion paths, merge/storage design, quarantine vs. flags, project layout, out-of-scope list. |
| `architecture.mermaid` / `.svg` / `.png` | Pipeline diagram (mermaid source + rendered exports). |
| `student.schema.json` | JSON Schema for the canonical record — **generated** from `src/models.py` via `scripts/gen_schema.py`; never edit by hand. |
| `requirements.txt` | Runtime dependencies (pydantic, pandas, Flask, pypdf, OCR stack, …). |
| `requirements-dev.txt` | Test/lint tooling: pytest, pytest-cov, reportlab, pyflakes. |
| `constraints.txt` | Pinned versions of every runtime dependency for reproducible installs. |
| `pytest.ini` | Pytest config: `testpaths = tests`, quiet mode, warning filters. |
| `.coveragerc` | Coverage config: measure `src/`, omit `add_document.py`, exclude `main()`/`__main__` blocks. |
| `.gitignore` | Excludes `data/` (all applicant PII), `.env` secrets, Python/editor cruft. |
| `.github/workflows/ci.yml` | GitHub Actions: on push/PR, matrix Python 3.10–3.12, installs Tesseract, pyflakes lint, pytest with 80% coverage gate, uploads coverage.xml. |

### `src/` — the pipeline

| File | Contents |
|---|---|
| `__init__.py` | Package docstring, `SCHEMA_VERSION = "2.0.0"` (canonical record), silences noisy pypdf warnings. |
| `run.py` | CLI orchestrator: parse → normalize → validate → merge → store → summary; writes `data/reports/_summaries.md`; exposes `reindex()`. |
| `app.py` | Flask web UI. Routes for upload (CSV/Excel, ZIP, single documents), browsing/downloading summaries, quarantine view, reset/purge. Implements all security guardrails (CSRF, rate limits, Basic Auth, headers, audit log). No pipeline logic — orchestrates `src.run`. |
| `parse.py` | Stage 3, deterministic extraction: reads CSV **or** Excel via pandas into `{column: value}` dicts; cleans Excel float artifacts (`1001.0` → `1001`); collapses indexed columns (`name_0`, `name_1`, …) into grouped arrays. |
| `normalize.py` | Stage 4a: assembles the canonical record from a parsed row using candidate column-name lists (IELTS/TOEFL/GRE/education/programs/interests), normalizes GPA to a 4.0 scale with per-value auto-detection, records data-quality warnings. Deliberately never maps protected attributes. |
| `models.py` | Typed Pydantic v2 canonical model (`StudentRecord` + sub-models: GPA, EducationEntry, test scores, programs…). `extra="forbid"` everywhere; range checks; source of truth for `student.schema.json`. |
| `validate.py` | Thin wrapper exposing `validate_record(record) -> (is_valid, errors)` over `models.parse_record`. |
| `template.py` | Stage 5: renders the factual per-applicant summary (academic standing, interests, factual highlights, quoted SOP motivation). No scoring, nothing invented. |
| `merge.py` | Unified per-student record (schema v3.0.0): identity + `sources.{application,resume,sop,lors[]}` + provenance + warnings. `upsert_source()` is the single extension point for new source types. |
| `store.py` | SQLite store for unified records, keyed by `cas_id`. Atomic writes, `BEGIN IMMEDIATE` read-modify-write, WAL mode, graceful degradation on filesystems without POSIX locks. |
| `documents.py` | Phase-2 single-document ingestion: extracts text from PDF/DOCX/TXT/MD, builds a source section (word counts, analysis, recommender), merges by `cas_id`. Defines `DOC_TYPES = (sop, lor, resume)`. |
| `add_document.py` | CLI wrapper over `documents.py` (`python -m src.add_document --input … --type …`); infers `cas_id` from filename prefix. |
| `ingest_zip.py` | Batch ZIP ingestion of combined "Full Application" PDFs: validates the ZIP, enforces count/size limits, reads entries in memory (no zip-slip), segments each PDF, merges sections per student. |
| `packet.py` | Segments a combined PDF into resume/SOP/LOR/scores/ignore sections: per-page feature scores (headings, contact block, first-person density, form/thesis detectors) decoded jointly with Viterbi transition penalties; flags low confidence. |
| `ocr.py` | Optional local OCR for scanned pages: pypdfium2 render → grayscale/autocontrast/Otsu/deskew → Tesseract (or RapidOCR). Every step degrades gracefully; OCR never raises. |
| `analyze.py` | Deterministic text analysis for SOPs/LORs: word-boundary keyword matching against `config/keywords.yaml` (research areas, skills), length/structure flags, test-score & recommender extraction, Jaccard near-duplicate detection. |
| `ratings.py` | Reads CAS "REFERENCE RATINGS" Likert tables (1–5 across eight criteria) from digital PDFs by mapping check-glyph x-positions to column anchors via pypdfium2. Skips scanned forms. |

### `config/`

| File | Contents |
|---|---|
| `csv_field_map.yaml` | Raw-column → canonical-field mapping: scalars, indexed groups (IELTS/TOEFL/education/experience), custom-question substring matches, and GPA scale settings (`gpa_scale: null` = auto-detect, with the threshold table). Edit when the export layout changes. |
| `keywords.yaml` | Keyword taxonomy for SOP/LOR analysis: 10 research areas (Power Systems … Photonics), a skills list, and thresholds (e.g. `sop_min_words`). Edit for your department. |

### `scripts/`

| File | Contents |
|---|---|
| `gen_schema.py` | Regenerates `student.schema.json` from the Pydantic model: `python -m scripts.gen_schema`. |

### `templates/` — Jinja2 (Flask UI)

| File | Contents |
|---|---|
| `base.html` | Shared layout: nav, flash messages, CSP-compatible styling. |
| `index.html` | Dashboard: upload forms (CSV/Excel, ZIP, single document), student list, quarantine, clear/purge controls. |
| `student.html` | Per-student detail: summary, sources, ratings, data-to-verify flags. |
| `summaries.html` | All summaries in one page / download view. |

### `tests/`

| File | Contents |
|---|---|
| `test_pipeline.py` | Core pipeline suite: parsing (CSV/Excel edge cases), normalization, GPA scales, validation/quarantine, merge, packet segmentation (reportlab-generated PDFs), OCR path, storage. |
| `test_app_guardrails.py` | 24 security tests for the Flask UI: extension/magic-byte rejection, 413 cap, same-origin 403, CSRF, rate-limit 429, Basic Auth, audit log, error logging, purge, protected-attribute enforcement. |
| `test_fixes.py` | Regression tests for specific fixed bugs. |

### `data/` — local only, **gitignored**, never committed

`raw/` (uploaded CSV/Excel/ZIP/PDF originals), `students.db` (+ `-wal`) SQLite store, `reports/_summaries.md` generated report, `quarantine/` invalid rows, `audit.log` web-UI access log.

## 7. Common operations & troubleshooting

- **Re-running is safe**: pipelines merge into existing records by `cas_id` (upsert), they don't overwrite.
- **Wrong GPA scale?** Force one via `gpa.gpa_scale` in `config/csv_field_map.yaml`.
- **Scanned PDF pages come back empty**: install Tesseract (see §4); pages are flagged, not lost.
- **"disk I/O error" on a synced/network drive**: `store.py` auto-degrades WAL → journal → in-process lock; prefer a local disk.
- **Changed `src/models.py`?** Regenerate the schema: `python -m scripts.gen_schema`.
- **New CSV layout?** Update `config/csv_field_map.yaml` and/or the candidate lists in `normalize.py`.
