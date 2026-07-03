# Graduate Applicant Pipeline — Architecture

## What it is

A fully **local, deterministic** pipeline that turns graduate-application data
(CSV/Excel) and supporting documents (resumes, SOPs, LORs — as single files, one
combined "Full Application" PDF, or a ZIP of many packets) into **one validated
JSON record per student**, then a factual descriptive summary. It describes
applicants — it does **not** score, rank, or judge them, and it never sends data
to an LLM or over the network.

## Principles

- **Local & deterministic.** No LLM and no network calls anywhere. Same input always yields the same output.
- **One record per student**, keyed by `cas_id`. Application data, resume, SOP, and LOR(s) merge into the same record so everything about a student lives in one place; records keyed by email vs. `cas_id` are reconciled.
- **Describe, don't decide.** The output is a neutral summary; the reviewer makes the call.
- **Fairness by construction.** Protected attributes (race, gender, age, citizenship, phone) are never parsed into the record.
- **Strong validation.** A typed Pydantic model coerces, range-checks, and forbids unknown fields; bad rows are quarantined with readable reasons.
- **Durable & concurrency-safe.** Records live in a local SQLite store; each merge is an atomic, lock-safe transaction, so concurrent uploads can't corrupt or lose a record.

## Diagram

See `architecture.svg` / `architecture.png` (rendered) and `architecture.mermaid` (source).

Two ingest paths feed one record:

```
CSV/Excel ──────── parse ─ normalize ─ strong-validate ───────────┐
                                                                  ├─ merge by cas_id ─ reconcile ─ Unified Student JSON ─ summary ─ UI
SOP / LOR / Resume ─ extract text (OCR fallback) ──┐              │
Combined PDF / ZIP ─ extract text ─ segment ───────┴─ analysis ───┘
```

Documents reach the pipeline three ways: as a single file (SOP / LOR / resume),
as one combined "Full Application" PDF that the segmenter splits into sections,
or as a ZIP of many such packets processed in one batch.

## Application path (CSV/Excel → application section)

1. **`parse.py`** — reads CSV *or* Excel with pandas (robust to quoting, encodings, BOM, Excel numeric-ID quirks); collapses indexed columns (`name_0`, `name_1`, …) into arrays.
2. **`normalize.py`** — maps source columns to canonical fields via candidate name-lists (tolerant of different exports), picks the identifier (`cas_id` → `email` → generated), normalizes GPA to a 4.0 scale (scale auto-detected per value, since it varies by university), and records data-quality warnings. Protected attributes are dropped here.
3. **`models.py` + `validate.py`** — the strong parser (Pydantic v2): coercion, range checks, `extra="forbid"`. Valid rows become the **application** section; invalid rows go to `data/quarantine/`. The model generates `student.schema.json` (`scripts/gen_schema.py`).

## Document path (SOP / LOR / Resume → their own sections, local & no LLM)

1. **`documents.py`** — extracts plain text from PDF, DOCX, or TXT and packages it as a source section. Supported types: `sop`, `lor`, `resume`. Single-file ingest (`ingest_document`) infers the `cas_id` from the filename prefix (`<cas_id>_sop.pdf`) and, for SOPs, runs a near-duplicate check against every other applicant's SOP.
2. **`ocr.py`** — text extraction for PDFs, with an OCR fallback. A page is treated as scanned (and OCR'd) when its embedded text is too sparse to be a real text page — by **word count**, not a bare character threshold, because scanned pages routinely carry a little embedded text (a "Scanned by CamScanner" watermark, a page number, a header) that defeats a naive threshold while the real content is an image. The rule: OCR if the page has very few words, or has a raster image (detected quickly via the page's XObject resources, no decode) and still few words. Scanned pages are rasterized at 300 DPI with `pypdfium2`, cleaned (grayscale → autocontrast → Otsu binarization → small-angle deskew, all with safe fallbacks), and read with Tesseract (`pytesseract`, LSTM engine); the **longer** of the embedded vs OCR'd text is kept, so a good text page can never be made worse. OCR runs **in parallel across pages** (a process pool, since Tesseract is single-threaded per call) with a per-page timeout and a per-document page cap. If Tesseract isn't installed the pipeline still runs and flags the affected pages rather than failing. OCR usage is recorded on the record (not treated as an error), and OCR'd pages are surfaced as data-to-verify since recognition is never perfect.
3. **`packet.py`** — admissions exports often bundle a student's resume, SOP, letters, score cards, transcripts and sometimes an entire **thesis** into **one** PDF. `segment()` labels every page with a small, deterministic sequence model (no LLM): each page gets an independent **emission score** for every label (resume / sop / lor / scores / ignore) from a bundle of features — heading phrases, a contact block, multi-word first-person prose density, LOR/reference form fields **and narrative recommendation-letter cues** (salutation/closing + recommend verbs + third-person about the applicant, so a first-person reference letter isn't mistaken for an SOP), score-report & transcript cues, and form/thesis/math detectors — and the page sequence is then **Viterbi-decoded** with transition penalties so sections come out as contiguous blocks. This replaced an earlier "carry the previous label forward" rule that dumped theses and form pages into whatever section preceded them. Resume/SOP/LOR/scores are kept; forms, boilerplate and thesis/appendix pages go to `ignore` and are dropped. A section that ends up spanning non-adjacent pages is flagged for review.
4. **`ratings.py`** — reads the CAS **"REFERENCE RATINGS" Likert tables** (Average=1 … Exceptional=5). The rating is a check-glyph whose *column* — and therefore value — is lost by plain text extraction, so this reads the page with character **positions** (pypdfium2): it finds the column x-anchors from the "(1)…(5)" markers (rejecting phone numbers/dates that merely contain those digits via an even-spacing test), then maps each check-glyph's x to its column. It returns each evaluator's name, per-criterion ratings, average, and recommendation phrase, plus the **average across all evaluators**. Works on digital CAS forms; a scanned evaluation has no glyph positions and is skipped.
5. **`ingest_zip.py`** — batch-ingests a **ZIP of combined packets** in one pass: `cas_id` (and a best-effort name) come from each filename, each PDF is segmented and analyzed, the Likert ratings are read and attached to the LOR source, and sections merge into the right student record. Safe by design — validates the archive, enforces file-count/size limits, and reads each entry in memory (no disk extraction → no zip-slip).
5. **`analyze.py`** — deterministic analysis of the extracted text:
   - **research areas** and **skills/tools** detected via the keyword taxonomy in `config/keywords.yaml`;
   - **length/structure** stats (word/sentence counts, avg sentence length);
   - **quality flags** (too short, no recognized area, LOR missing a recommender);
   - **near-duplicate SOP detection** across applicants (trigram Jaccard) to flag possible reuse;
   - **motivation sentences** quoted verbatim from an SOP (the applicant's own words — selected, never interpreted);
   - **recommender extraction** from LOR text (the CAS `REFERENCES` block, signature closings, or a `Professor/Dr.` fallback) — name, title, and organization;
   - **test-score extraction** from two sources, reported as written: ETS-style score reports (`extract_test_scores`) and the CAS academic-history "STANDARDIZED TESTS" tables (`extract_standardized_tests`, which reads the GRE/IELTS/TOEFL value rows, stripping interleaved percentile columns). These fold into the academic-standing line marked "(parsed)".

   This is keyword + structure + pattern extraction, **not** interpretation of the writing.

   Score reports and transcripts (the scanned CamScanner pages, GRE/TOEFL reports) are **no longer dropped**: they are OCR'd, captured as a `scores` source, and their parsed figures surfaced in the summary (flagged "verify against official records").

## Merge & the unified record (`merge.py`)

Each source is attached to the student's record by `cas_id`:

```json
{
  "schema_version": "3.0.0",
  "student_id": "<cas_id>",
  "identity": { "full_name": "...", "email": "..." },
  "sources": {
    "application": { "programs": [...], "education": [...], "english_proficiency": {...}, "gre_results": [...], "interests": {...} },
    "resume": { "text": "...", "word_count": 0, "analysis": {...} },
    "sop":  { "text": "...", "word_count": 0, "excerpt": "...", "analysis": { "detected_areas": [...], "mentioned_skills": [...], "flags": [...] } },
    "scores": { "text": "...", "analysis": { "test_scores": { "gre_verbal": 155, "gre_quant": 168, "gre_awa": 4.5, "gre_total": 323 } } },
    "lors": [ { "text": "...", "recommender": "...", "recommender_title": "...", "recommender_organization": "...", "analysis": {...} } ]
  },
  "provenance": [ { "source": "application", "file": "...", "row": 0, "ingested_at": "..." } ],
  "warnings": [ "..." ]
}
```

Merging is additive: a new file fills missing fields without wiping what a prior
file set; different source types live in separate sections so they never collide;
LORs append. Re-running, or running a different source file, updates the same
record on disk.

**Reconcile (`merge.reconcile`).** The CSV may key a student by `email` (when the
export has no `cas_id` column) while the PDF packet keys the same person by their
numeric `cas_id`. After ingest, `reconcile()` folds the email/`ROW`-keyed record
into the matching `cas_id`-keyed one when their names agree. It is deliberately
conservative: it only collapses non-`cas_id` keys into a matching `cas_id` key,
never two distinct `cas_id`s. This runs automatically inside `reindex()` after any
source is ingested.

## Storage (`store.py`)

Records persist in a local **SQLite** database (`data/students.db`) — one row per
student, the unified record kept as JSON, plus a quarantine table. SQLite is an
in-process file database, so the pipeline stays fully local with no server or
network. Two properties matter:

- **Atomic writes.** A record is written in a single transaction, so a crash
  mid-write can never leave a half-written record (the earlier one-JSON-file-per-student
  writer could).
- **Concurrency-safe merges.** Every ingest path mutates a record through
  `Store.update(cas_id, fn)`, which opens a `BEGIN IMMEDIATE` (write-lock)
  transaction, reads the current record, applies the change, and writes it back
  under the same lock — plus a per-path in-process lock. Concurrent writers to the
  same student therefore serialize instead of overwriting each other ("lost
  update"). WAL mode keeps the web UI readable while a write is in flight, and
  `busy_timeout` makes contending writers wait rather than fail.
- **Filesystem robustness.** On a normal local disk the store uses WAL. Some
  networked/synced/FUSE mounts can't provide WAL's shared memory or POSIX locks
  (writes raise "disk I/O error"); the store probes this at startup and degrades
  WAL → rollback journal → a nolock mode where the in-process lock provides
  serialization. If a directory can't host SQLite at all, the database is
  relocated to a local working directory (the human-readable report still writes
  to the chosen folder) and a one-line notice is printed.

## Output

- **`template.py`** — renders a neutral factual summary per student from whatever sources are present (gets richer as documents are added). Surfaces SOP-detected areas/skills with an explicit "keyword detection, not interpretation" caveat.
- **`app.py`** (Flask) — upload CSV/Excel; attach a single SOP/LOR/resume per student; upload a ZIP of combined packets; a combined `/process` route that takes a CSV **and** a ZIP in one submit (CSV first, then packets merge by `cas_id`); a `/reset` route to clear processed data (optionally purging raw uploads too); browse applicants; per-student detail (identity, programs, education, test scores, sources-on-file, document analysis); data-to-verify flags; quarantine view; written summaries; download. No login by default (single-reviewer, localhost tool) but ships with upload-size caps, extension allow-lists, a same-origin/CSRF check, and opt-in Basic Auth / remote-binding guards — see "Security & privacy guardrails" in `README.md`.
- **`run.py`** / **`add_document.py`** / **`ingest_zip.py`** — CLIs for the application path, single-document ingest, and ZIP-batch ingest. `run.py` also owns `reindex()` (rebuilds `students/_all.json` and `reports/_summaries.md`, and runs `reconcile`).

## Data-quality flags vs. quarantine

- **Quarantine** — the row failed validation (e.g. an out-of-range value) and could not be recorded; shown with reasons.
- **Data-to-verify flag** — the record is valid but something is missing or unclear (e.g. "No GRE score on file", "SOP very short", "no cas_id — keyed by email"). Factual notes for the reviewer, not judgements.

## Project layout

```
grad app/
├── ARCHITECTURE.md · architecture.svg/.png/.mermaid · README.md
├── student.schema.json            # generated from src/models.py
├── config/
│   ├── csv_field_map.yaml          # column mapping + GPA scale rules
│   └── keywords.yaml               # research-area & skill taxonomy
├── scripts/gen_schema.py
├── src/
│   ├── parse.py  normalize.py  models.py  validate.py     # application path
│   ├── documents.py  analyze.py  ocr.py  packet.py        # document path (local, no LLM)
│   ├── ratings.py                                         # CAS Likert reference-rating reader
│   ├── ingest_zip.py                                      # batch ingest a ZIP of packets
│   ├── merge.py                                           # unified record + merge helpers
│   ├── store.py                                           # SQLite store: atomic upsert + reconcile
│   ├── template.py                                        # descriptive summary
│   ├── run.py  add_document.py                            # CLIs
│   └── app.py                                             # Flask web UI
├── templates/  (base, index, student, summaries)
├── data/  raw/ (uploads) · students.db (SQLite store) · reports/_summaries.md
└── tests/test_pipeline.py
```

## Deliberately out of scope

Genuine interpretation of SOP/LOR writing — assessing motivation, narrative
quality, or fit — would require a language model and is intentionally **not**
done, to keep the system local, private, and free of automated judgement.
