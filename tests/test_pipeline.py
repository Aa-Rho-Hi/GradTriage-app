"""End-to-end + unit checks for the deterministic pipeline."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from src.normalize import build_record
from src.parse import read_rows, group_indexed
from src.validate import validate_record
from src.template import render
from src.run import run

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = yaml.safe_load(open(os.path.join(ROOT, "config", "csv_field_map.yaml")))
SCHEMA = os.path.join(ROOT, "student.schema.json")
CSV = os.path.join(ROOT, "data", "raw", "sample_applications.csv")


def _rec(cas_id):
    for r in read_rows(CSV):
        rec = build_record(r, CFG, "sample.csv", 0)
        if rec["cas_id"] == cas_id:
            return rec
    raise AssertionError(cas_id)


def test_indexed_grouping():
    _, groups = group_indexed({"ielts_overall_band_score_0": "7.0",
                               "first_name": "X",
                               "gpas_by_transcript_gpa_2": "3.5"})
    assert groups["ielts_overall_band_score"][0] == "7.0"
    assert groups["gpas_by_transcript_gpa"][2] == "3.5"


def test_ielts_attempts_and_best():
    rec = _rec("CAS1001")
    eng = rec["english_proficiency"]
    assert len(eng["ielts"]) == 2
    assert eng["best_ielts_overall"] == 7.5


def test_gpa_scale_detection():
    # 8.6 -> 10-point scale; 3.7 -> 4-point scale
    g1 = _rec("CAS1001")["education"][0]["gpa"]
    assert g1["scale"] == 10.0 and 3.4 <= g1["normalized_4"] <= 3.5
    g2 = _rec("CAS1002")["education"][0]["gpa"]
    assert g2["scale"] == 4.0 and g2["normalized_4"] == 3.7
    g3 = _rec("CAS1003")["education"][0]["gpa"]
    assert g3["scale"] == 100.0  # 72 -> percentage


def test_toefl_total_parsed():
    rec = _rec("CAS1002")
    assert rec["english_proficiency"]["best_toefl_total"] == 112.0


def test_summary_is_descriptive_not_scored():
    rec = _rec("CAS1001")
    text = render(rec)
    assert rec["personal"]["full_name"].split()[0] in text
    # neutral summary: no score/fit/recommendation language
    low = text.lower()
    assert "fit score" not in low and "/100" not in low
    assert "why consider" not in low and "strong candidate" not in low


def test_all_valid_against_schema():
    for r in read_rows(CSV):
        rec = build_record(r, CFG, "sample.csv", 0)
        ok, errors = validate_record(rec, SCHEMA)
        assert ok, errors


def test_run_builds_unified_student(tmp_path):
    from src.store import Store, db_path_for
    summary = run(CSV, str(tmp_path),
                  os.path.join(ROOT, "config", "csv_field_map.yaml"))
    assert summary["valid"] == 4 and summary["quarantined"] == 0
    store = Store(db_path_for(str(tmp_path)))
    u = store.get("CAS1001")
    assert u is not None
    # unified shape: identity + sectioned sources + provenance
    assert u["student_id"] == "CAS1001"
    assert u["sources"]["application"]["education"]
    assert u["sources"]["sop"] is None and u["sources"]["lors"] == []
    assert u["provenance"][0]["source"] == "application"
    assert u["summary"]
    assert store.count() == 4 and len(store.index()) == 4


def test_merge_accumulates_across_runs(tmp_path):
    from src.merge import upsert_source
    from src.store import Store, db_path_for
    # simulate: CSV run already wrote a student, then an SOP arrives
    run(CSV, str(tmp_path), os.path.join(ROOT, "config", "csv_field_map.yaml"))
    store = Store(db_path_for(str(tmp_path)))

    def _add_sop(u):
        upsert_source(u, "sop", {"text": "x", "word_count": 1}, file="s.txt")
        return u

    u = store.update("CAS1001", _add_sop)
    assert u["sources"]["application"] is not None     # CSV section preserved
    assert u["sources"]["sop"] == {"text": "x", "word_count": 1}  # new section added
    assert len(u["provenance"]) == 2
    # persisted, not just in memory
    assert store.get("CAS1001")["sources"]["sop"]["text"] == "x"


def test_sop_analysis_detects_areas_and_flags():
    from src.analyze import analyze_text
    a = analyze_text("My research is in VLSI and machine learning using Verilog and PyTorch. "
                     "I designed CMOS chips and neural networks.", "sop")
    assert "VLSI / Microelectronics" in a["detected_areas"]
    assert "Machine Learning / AI" in a["detected_areas"]
    assert "verilog" in a["mentioned_skills"] and "pytorch" in a["mentioned_skills"]
    # short SOP -> flagged
    assert any("very short" in f for f in a["flags"])


def test_document_analysis_extracts_review_profile():
    from src.analyze import analyze_text
    resume = (
        "Education\nMichigan State University\nBachelor of Science in Electrical Engineering\n"
        "Graduated with Honor GPA: 3.81\nGRE: 317 (V 148; Q 169)\n"
        "Skills: Python, Matlab, C++, SPICE, PCB design\n"
        "Research Experience\nOptimized a power electronics model and simulated circuits in Matlab.\n"
        "Course Project\nDeveloped a microcontroller project for signal acquisition.\n"
        "Won the department design award.\n"
    )
    rp = analyze_text(resume, "resume")["profile"]
    assert any("GPA: 3.81" in x for x in rp["education"])
    assert any("GRE: 317" in x for x in rp["test_scores"])
    assert rp["technical_preparation"]
    assert rp["experience"]
    assert rp["projects"]
    assert rp["distinctions"]

    sop = (
        "My goal is to research power electronics and improve grid reliability for the future. "
        "During my undergraduate laboratory research I simulated converter controls and learned "
        "measurement techniques that shaped my design intuition. "
        "The department faculty and research group at Texas A&M University align closely with my interests. "
        "Eventually I see myself contributing to the renewable energy industry and its growing workforce."
    )
    sp = analyze_text(sop, "sop")["profile"]
    assert sp["goals"] and sp["preparation"] and sp["program_fit"] and sp["career_direction"]

    lor = (
        "Dear Admissions Committee. I have known the applicant for two years in my research lab. "
        "He completed an independent project, showed strong analytical ability, and I strongly "
        "recommend him for graduate study."
    )
    lp = analyze_text(lor, "lor")["profile"]
    assert lp["evidence"]


def test_summary_surfaces_detailed_application_materials():
    """The descriptive summary surfaces résumé, SOP and recommendation detail for
    decision-making."""
    from src.documents import build_section
    resume = build_section("resume", (
        "Education\nMichigan State University\nBachelor of Science in Electrical Engineering\n"
        "Graduated with Honor GPA: 3.81\nGRE: 317 (V 148; Q 169)\n"
        "Skills: Python, Matlab, C++, SPICE, PCB design\n"
        "Research Experience\nOptimized a power electronics model and simulated circuits in Matlab.\n"
        "Course Project\nDeveloped a microcontroller project for signal acquisition.\n"
    ), "resume.txt")
    sop = build_section("sop", (
        "My goal is to study power systems at Texas A&M University. "
        "In my undergraduate research project, I simulated converter controls in Matlab and learned "
        "how laboratory measurements affect design decisions. "
        "I plan to use the graduate program to prepare for a career improving reliable energy systems."
    ), "sop.txt")
    lor = build_section("lor", (
        "Dear Admissions Committee. I have known the applicant for two years in my research lab. "
        "He completed an independent project, showed strong analytical ability, and I strongly "
        "recommend him for graduate study. Sincerely, Dr. Jane Smith."
    ), "lor.txt", recommender="Dr. Jane Smith")
    rec = {
        "cas_id": "CAS-RICH",
        "personal": {"full_name": "Detailed Applicant"},
        "_documents": {"resume": resume, "sop": sop, "scores": None, "lors": [lor]},
        "meta": {"warnings": []},
    }
    text = render(rec)
    assert "Resume detail" in text and "Graduated with Honor GPA: 3.81" in text
    assert "SOP detail" in text and "Texas A&M University" in text
    assert "Recommendation detail" in text and "Dr. Jane Smith" in text
    low = text.lower()
    assert "fit score" not in low and "/100" not in low and "strong candidate" not in low


def test_reindex_refreshes_existing_summaries(tmp_path):
    from src.documents import build_section
    from src.merge import new_student, summary_view, upsert_source
    from src.run import reindex
    from src.store import Store, db_path_for
    store = Store(db_path_for(str(tmp_path)))
    u = new_student("CAS-REFRESH")
    section = build_section("resume", (
        "Education\nState University\nBachelor of Science in Electrical Engineering\n"
        "GPA: 3.7\nSkills: Python and Matlab\nResearch Experience\n"
        "Developed a power systems simulation project."
    ), "resume.txt")
    section["analysis"].pop("profile")                 # simulate a pre-upgrade stored record
    upsert_source(u, "resume", section, file="resume.txt")
    u["summary"] = "OLD SUMMARY"
    store.put(u)
    reindex(str(tmp_path))
    refreshed = Store(db_path_for(str(tmp_path))).get("CAS-REFRESH")
    assert "OLD SUMMARY" not in refreshed["summary"]
    assert "Resume detail" in refreshed["summary"]
    assert render(summary_view(refreshed)) == refreshed["summary"]


def test_duplicate_sop_detection(tmp_path):
    from src.store import Store, db_path_for
    run(CSV, str(tmp_path), os.path.join(ROOT, "config", "csv_field_map.yaml"))
    essay = ("I am deeply passionate about power systems and renewable energy. "
             "My goal is to research smart grids and advance the field. ") * 8
    ingest_document_txt(tmp_path, "CAS1001", essay)
    ingest_document_txt(tmp_path, "CAS1002", essay)   # same essay -> should flag CAS1002
    u = Store(db_path_for(str(tmp_path))).get("CAS1002")
    flags = u["sources"]["sop"]["analysis"]["flags"]
    assert any("similar" in f.lower() for f in flags)


def ingest_document_txt(tmp_path, cas_id, text):
    from src.documents import ingest_document
    p = tmp_path / f"{cas_id}_sop.txt"
    p.write_text(text)
    ingest_document(str(p), cas_id, "sop", str(tmp_path))


def test_document_ingest_merges_sop(tmp_path):
    from src.documents import ingest_document
    run(CSV, str(tmp_path), os.path.join(ROOT, "config", "csv_field_map.yaml"))
    sop = tmp_path / "CAS1001_sop.txt"
    sop.write_text("I am passionate about power systems and renewable energy research. " * 20)
    unified, words = ingest_document(str(sop), "CAS1001", "sop", str(tmp_path))
    assert words > 50
    assert unified["sources"]["application"] is not None      # CSV section kept
    assert unified["sources"]["sop"]["word_count"] == words    # SOP section added
    low = unified["summary"].lower()
    assert "sop" in low and "motivation" in low                # SOP surfaced in description
    # provenance records both sources
    assert {p["source"] for p in unified["provenance"]} == {"application", "sop"}


def test_reconcile_merges_email_and_cas_records(tmp_path):
    from src.merge import new_student, upsert_source
    from src.store import Store, db_path_for
    store = Store(db_path_for(str(tmp_path)))
    # same person under two keys: email (CSV) and numeric cas_id (PDF)
    a = new_student("jane@x.com"); a["identity"]["full_name"] = "Jane Doe"
    upsert_source(a, "application", {"programs": [{"name": "MS EE"}]}, file="app.csv")
    b = new_student("1000123456"); b["identity"]["full_name"] = "Jane Doe"
    upsert_source(b, "sop", {"text": "hi", "word_count": 1}, file="x.pdf")
    store.put(a); store.put(b)
    merged = store.reconcile()
    assert merged == 1
    ids = [u["student_id"] for u in store.all()]
    assert ids == ["1000123456"]                             # kept the cas_id key
    u = store.get("1000123456")
    assert u["sources"]["application"] and u["sources"]["sop"]  # both sources merged


def test_concurrent_updates_do_not_lose_data(tmp_path):
    """Two writers hammering the same cas_id must not clobber each other —
    every append must survive (no lost update)."""
    import threading
    from src.merge import new_student
    from src.store import Store, db_path_for
    store = Store(db_path_for(str(tmp_path)))
    store.put(new_student("CAS9999"))
    N = 40

    def append(i):
        store.update("CAS9999",
                     lambda u: u["warnings"].append(f"note-{i}") or u)

    threads = [threading.Thread(target=append, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    u = store.get("CAS9999")
    assert len(u["warnings"]) == N                            # all writes survived
    assert len(set(u["warnings"])) == N


def test_ocr_reads_scanned_pdf():
    import pytest, io
    pytest.importorskip("PIL")
    pytest.importorskip("pypdfium2")
    from src import ocr
    if not ocr.available():
        pytest.skip("tesseract not installed")
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1200, 400), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 44)
    except Exception:
        font = ImageFont.load_default()
    d.text((40, 120), "power systems and machine learning", fill="black", font=font)
    buf = io.BytesIO(); img.save(buf, format="PDF")
    pages, info = ocr.pdf_page_texts(buf.getvalue())
    assert info["ocr_used"] == 1
    assert "power systems" in pages[0].lower()


def test_cas_id_and_name_from_filename():
    from src.packet import cas_id_from_filename, name_from_filename
    fn = "1000419138_Joonha_Jun_Full Application_200948_Fall 2020 M.S. EE.pdf"
    assert cas_id_from_filename(fn) == "1000419138"
    nm = name_from_filename(fn)
    assert nm["first_name"] == "Joonha" and nm["last_name"] == "Jun"


def _make_packet(path, name, sop_text, resume_text):
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import LETTER
    c = canvas.Canvas(path, pagesize=LETTER)
    for title, body in [("Application for Admission", f"Applicant: {name}"),
                        ("Statement of Purpose", sop_text),
                        ("Curriculum Vitae", resume_text),
                        ("IELTS Test Report Form", "Overall Band Score 7.5")]:
        c.setFont("Helvetica-Bold", 16); c.drawString(72, 720, title)
        c.setFont("Helvetica", 11); c.drawString(72, 690, body)
        c.showPage()
    c.save()


def test_zip_ingestion_segments_and_merges(tmp_path):
    import pytest, zipfile
    pytest.importorskip("reportlab")
    from src.ingest_zip import ingest_zip
    from src.store import Store, db_path_for
    pdf = tmp_path / "1000419138_Joonha_Jun_Full Application_1_Fall 2020 EE.pdf"
    _make_packet(str(pdf), "Joonha Jun",
                 "My research is in power systems and smart grid renewable energy. " * 10,
                 "Skills: python, matlab, verilog. Experience: research intern. " * 5)
    zpath = tmp_path / "packets.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.write(pdf, pdf.name)
    report = ingest_zip(str(zpath), str(tmp_path))
    assert report["pdfs_found"] == 1 and report["students_updated"] == 1
    u = Store(db_path_for(str(tmp_path))).get("1000419138")
    assert u["sources"]["sop"] and u["sources"]["resume"]          # both kept
    assert "Power Systems" in u["sources"]["sop"]["analysis"]["detected_areas"]
    assert "verilog" in u["sources"]["resume"]["analysis"]["mentioned_skills"]
    assert u["identity"]["full_name"] == "Joonha Jun"              # name from filename
    assert u["sources"]["scores"]                                  # IELTS page kept as scores


def test_extract_recommender_and_scores():
    from src.analyze import extract_recommender, extract_test_scores
    lor = ("REFERENCES\nSarang Dhongdi\nProfessional Title: Assistant Professor\n"
           "Organization: BITS Pilani K K Birla Goa Campus\nEmail: x@y.edu\n")
    rec = extract_recommender(lor)
    assert rec["name"] == "Sarang Dhongdi"
    assert rec["title"] == "Assistant Professor"
    assert "BITS Pilani" in rec["organization"]
    gre = ("verbal reasoning with your scaled score of 155 out of 170. "
           "quantitative reasoning with your scaled score of 168 out of 170. "
           "analytical writing with your score of 4.5 out of 6.")
    ts = extract_test_scores(gre)
    assert ts["gre_verbal"] == 155 and ts["gre_quant"] == 168
    assert ts["gre_awa"] == 4.5 and ts["gre_total"] == 323
    assert extract_test_scores("Overall Band Score 7.5")["ielts_overall"] == 7.5
    assert extract_recommender("no references here at all") == {}


# --------------------------------------------------------------------------
# Edge cases
# --------------------------------------------------------------------------

def _write_csv(path, header, *rows):
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n")
        for r in rows:
            f.write(r + "\n")


def test_invalid_row_is_quarantined(tmp_path):
    """A value outside the allowed range (IELTS > 9) must be quarantined with a
    readable reason — not silently coerced or dropped — while valid rows survive."""
    from src.store import Store, db_path_for
    csv = tmp_path / "mixed.csv"
    _write_csv(str(csv),
               "cas_id,first_name,last_name,ielts_overall_band_score_0",
               "CAS5001,Bad,Row,11.0",       # 11.0 > 9 -> invalid
               "CAS5002,Good,Row,7.0")        # fine
    field_map = os.path.join(ROOT, "config", "csv_field_map.yaml")
    summary = run(str(csv), str(tmp_path), field_map)
    assert summary["valid"] == 1 and summary["quarantined"] == 1
    store = Store(db_path_for(str(tmp_path)))
    assert store.get("CAS5002") is not None and store.get("CAS5001") is None
    q = store.quarantine()
    assert len(q) == 1 and q[0]["cas_id"] == "CAS5001"
    assert any("less than or equal to 9" in e for e in q[0]["errors"])


def test_corrupt_pdf_in_zip_is_reported_not_fatal(tmp_path):
    """A non-PDF / corrupt file in the ZIP is reported as unmatched and does not
    crash the batch or create a record."""
    import zipfile
    from src.ingest_zip import ingest_zip
    from src.store import Store, db_path_for
    zpath = tmp_path / "packets.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("1000222222_Bad_File_Full Application_x.pdf", b"this is not a pdf")
    report = ingest_zip(str(zpath), str(tmp_path))
    assert report["pdfs_found"] == 1
    assert report["students_updated"] == 0
    assert report["unmatched_files"]                       # reported, not raised
    assert Store(db_path_for(str(tmp_path))).count() == 0


def test_duplicate_cas_id_csv_then_zip_merges_one_record(tmp_path):
    """The same numeric cas_id arriving from the application path and from a PDF
    packet must end up as ONE record holding both sources — no duplicate."""
    import pytest, zipfile
    pytest.importorskip("reportlab")
    from src.merge import new_student, upsert_source
    from src.ingest_zip import ingest_zip
    from src.store import Store, db_path_for
    store = Store(db_path_for(str(tmp_path)))
    # application section already on disk for this numeric id
    a = new_student("1000419138")
    a["identity"]["full_name"] = "Joonha Jun"
    upsert_source(a, "application", {"programs": [{"name": "MS EE"}]}, file="app.csv")
    store.put(a)
    # now a packet for the SAME id arrives
    pdf = tmp_path / "1000419138_Joonha_Jun_Full Application_1_Fall 2020 EE.pdf"
    _make_packet(str(pdf), "Joonha Jun",
                 "My research is in power systems and smart grid renewable energy. " * 10,
                 "Skills: python, matlab, verilog. Experience: research intern. " * 5)
    zpath = tmp_path / "p.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.write(pdf, pdf.name)
    ingest_zip(str(zpath), str(tmp_path))
    assert store.count() == 1                               # not duplicated
    u = store.get("1000419138")
    assert u["sources"]["application"] and u["sources"]["sop"] and u["sources"]["resume"]


def _make_headingless_packet(path):
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import LETTER
    c = canvas.Canvas(path, pagesize=LETTER)
    # page 1 — resume with NO heading, just a contact block
    lines1 = ["Jane Doe", "jane.doe@example.com  +1 415 555 1234",
              "San Francisco, CA", "Skills: python, matlab, verilog",
              "Experience: research intern at a power lab"]
    y = 720
    for ln in lines1:
        c.setFont("Helvetica", 12); c.drawString(72, y, ln); y -= 20
    c.showPage()
    # page 2 — statement with NO heading, just prose full of SOP cues
    sop = ("My research interest is in power systems and renewable energy. "
           "I am applying to the masters program because my goal is to study smart grids. "
           "I plan to pursue a master degree and my motivation is to advance the field. "
           "In my undergraduate I built models and my interest in control grew. "
           "I would love to research grid stability and I intend to contribute to the area. ") * 2
    y = 720
    for chunk in [sop[i:i+90] for i in range(0, len(sop), 90)]:
        c.setFont("Helvetica", 11); c.drawString(60, y, chunk); y -= 16
    c.showPage()
    c.save()


def test_headingless_packet_segments_by_heuristics(tmp_path):
    """A packet with no section headings — resume is just a contact block, the
    statement is just prose — is still recovered via the content heuristics."""
    import pytest
    pytest.importorskip("reportlab")
    from src.packet import segment
    pdf = tmp_path / "headingless.pdf"
    _make_headingless_packet(str(pdf))
    seg = segment(str(pdf))
    assert "resume" in seg["sections"]                     # recovered by contact block
    assert "sop" in seg["sections"]                        # recovered by prose + cues


def test_mixed_gpa_scales_normalize_to_4(tmp_path):
    """GPAs on different university scales each auto-detect and normalize to /4.0."""
    from src.normalize import _normalize_gpa
    cases = {4.0: 3.9, 5.0: 4.6, 10.0: 8.6, 20.0: 18.0, 100.0: 72.0}
    for expected_scale, raw in cases.items():
        g = _normalize_gpa(raw, CFG)
        assert g["scale"] == expected_scale, (raw, g)
        assert 0.0 <= g["normalized_4"] <= 4.0
    # a 10-scale 8.6 lands around 3.44/4.0; a 4-scale 3.9 stays ~3.9
    assert abs(_normalize_gpa(8.6, CFG)["normalized_4"] - 3.44) < 0.05
    assert abs(_normalize_gpa(3.9, CFG)["normalized_4"] - 3.9) < 0.05


def test_store_works_without_os_locks(tmp_path, monkeypatch):
    """On a filesystem with no POSIX locks (some network/FUSE mounts), the store
    falls back to nolock + in-process serialization and still reads/writes/persists."""
    from src.store import Store, db_path_for
    from src.merge import new_student
    # force the 'filesystem can't lock' branch
    monkeypatch.setattr(Store, "_fs_supports_locks", lambda self: False)
    s = Store(db_path_for(str(tmp_path)))
    assert s._cfg["nolock"] is True and s._cfg["journal"] == "off"
    s.put(new_student("X1"))
    s.update("X1", lambda u: u["warnings"].append("w") or u)
    rec = s.get("X1")
    assert rec is not None and rec["warnings"] == ["w"]
    assert s.count() == 1
    # a second Store on the same path sees the persisted record
    assert Store(db_path_for(str(tmp_path))).get("X1")["warnings"] == ["w"]


def test_ocr_reads_skewed_scanned_page(tmp_path):
    """A skewed image-only page is deskewed + preprocessed and still read."""
    import pytest, io
    pytest.importorskip("PIL")
    pytest.importorskip("pypdfium2")
    pytest.importorskip("numpy")
    from src import ocr
    if not ocr.available():
        pytest.skip("tesseract not installed")
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1600, 500), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 38)
    except Exception:
        font = ImageFont.load_default()
    d.text((40, 160), "power systems and machine learning", fill="black", font=font)
    img = img.rotate(3, resample=Image.BILINEAR, fillcolor="white", expand=True)
    buf = io.BytesIO(); img.save(buf, format="PDF")
    pages, info = ocr.pdf_page_texts(buf.getvalue())
    assert info["ocr_used"] == 1
    low = pages[0].lower()
    assert "power systems" in low and "machine learning" in low


def test_clean_ocr_text_fixes_common_artifacts():
    from src.ocr import clean_ocr_text
    assert clean_ocr_text("| have known Lekhaj") == "I have known Lekhaj"
    assert clean_ocr_text("ideas, | was able to observe") == "ideas, I was able to observe"
    assert clean_ocr_text("|n my view |t is fine") == "In my view It is fine"
    assert clean_ocr_text("the efﬁcient ﬂow is ﬁne") == "the efficient flow is fine"
    assert clean_ocr_text("collapse    spaces") == "collapse spaces"
    assert clean_ocr_text("code a||b stays") == "code a||b stays"   # not standalone


def test_otsu_binarize_is_deterministic():
    import pytest
    pytest.importorskip("numpy")
    from PIL import Image
    import numpy as np
    from src.ocr import _otsu_binarize
    arr = np.zeros((20, 20), dtype=np.uint8)
    arr[5:15, 5:15] = 200                      # a bright square on dark
    img = Image.fromarray(arr, mode="L")
    a = np.asarray(_otsu_binarize(img))
    b = np.asarray(_otsu_binarize(img))
    assert (a == b).all()                      # deterministic
    assert set(np.unique(a)).issubset({0, 255})


def test_extract_standardized_tests_cas_form():
    """The CAS academic-history GRE/IELTS/TOEFL tables (date row with interleaved
    percentiles) parse to the right figures."""
    from src.analyze import extract_standardized_tests
    text = (
        "STANDARDIZED TESTS\n"
        "OFFICIAL GRE\n"
        "Date ETS Registration Code Verbal Quantitative Analytical Writing\n"
        "09-20-2019 148 37% 164 84% 3.5 39%\n"
        "UNOFFICIAL IELTS\n"
        "Date Candidate Number TRF Number Listening Reading Speaking Writing Overall Band\n"
        "02-02-2019 012440 8.5 7.5 6.0 6.0 7.0\n"
        "OFFICIAL TOEFL\n"
        "Date Type Listening Reading Speaking Writing Essay Total\n"
        "09-28-2019 Internet-based 23 21 23 27 94\n"
    )
    ts = extract_standardized_tests(text)
    assert ts["gre_verbal"] == 148 and ts["gre_quant"] == 164 and ts["gre_awa"] == 3.5
    assert ts["gre_total"] == 312
    assert ts["ielts_overall"] == 7.0
    assert ts["toefl_total"] == 94


def test_likert_column_anchors_and_nearest():
    """The rating columns are found from evenly-spaced (1)-(5) markers, and a
    phone number / date that merely contains those digits is rejected."""
    from src.ratings import _column_anchors, _nearest_rating
    header = [(309.0, "1"), (358.0, "2"), (430.0, "3"), (509.0, "4"), (585.0, "5"),
              (120.0, "A"), (250.0, "x")]
    rows = {512: header}
    anchors, y = _column_anchors(rows)
    assert y == 512 and anchors == {1: 309.0, 2: 358.0, 3: 430.0, 4: 509.0, 5: 585.0}
    # checkmark x -> rating column
    assert _nearest_rating(561, anchors) == 5     # the real packet's marks
    assert _nearest_rating(483, anchors) == 4
    assert _nearest_rating(309, anchors) == 1
    # a phone number row "821030756824" -> digits not strictly increasing/even
    phone = [(float(100 + i * 7), ch) for i, ch in enumerate("821030756824")]
    assert _column_anchors({400: phone}) == (None, None)


def test_likert_name_and_recommendation_parsing():
    from src.ratings import _evaluator_name, _recommendation
    txt = ("EVALUATIONS\nEVALUATOR INFORMATION\nWongyu Bae\nTitle: Assistant Professor\n"
           "REFERENCE RATINGS\nI highly recommend this applicant\n")
    assert _evaluator_name(txt) == "Wongyu Bae"
    assert _recommendation(txt) == "i highly recommend"
    assert _recommendation("no rec phrase here") is None


def test_segmenter_labels_pages_by_content_not_neighbour():
    """The per-page scorer + Viterbi must label each page by its own content, so
    a thesis page or a duplicate resume can't bleed into an adjacent section."""
    from src.packet import _page_scores, _viterbi
    headers = CFG and __import__("yaml").safe_load(
        open(os.path.join(ROOT, "config", "keywords.yaml")))["section_headers"]
    pages = [
        "Application for Admission\nApplicant: X\nProgram Level: Masters\nStart Term: Fall",
        "John Doe\njohn@example.com  +1 415 555 1234\nEDUCATION\nEXPERIENCE\nSKILLS: python, matlab",
        ("Statement of Purpose\nMy research interest is in power systems. I am applying "
         "because my goal is to study smart grids. I plan to pursue research and my "
         "motivation is to advance the field. " * 3),
        ("Type: Letter of Reference and Likert Scale\nREFERENCES\nDr. Smith\n"
         "Professional Title: Professor\nWaiver of Evaluation: Yes\nI strongly recommend the applicant."),
        "Test Taker Score Report\nTOEFL\nVerbal Reasoning scaled score 160, 88th percentile\nTest Date: 2019",
        "Chapter 2\nMathematical Background\nBibliography\nList of Figures\n" + "∑ α β ∫ ≥ θ λ " * 5,
        "Jane Roe\njane@example.com  +1 212 555 9000\nWORK EXPERIENCE\nPROJECTS\nSKILLS: c++, verilog",
    ]
    labels = _viterbi([_page_scores(p, headers) for p in pages])
    assert labels[0] == "ignore"    # application form
    assert labels[1] == "resume"
    assert labels[2] == "sop"
    assert labels[3] == "lor"
    assert labels[4] == "scores"
    assert labels[5] == "ignore"    # thesis page dropped, NOT merged into scores/sop
    assert labels[6] == "resume"    # a resume-like page after others stays resume


def test_segmenter_distinguishes_recommendation_letter_from_sop():
    """A narrative recommendation letter is first-person too — it must still be
    labelled lor (salutation + third-person about the applicant + recommend),
    not sop."""
    from src.packet import _page_scores, _viterbi
    headers = __import__("yaml").safe_load(
        open(os.path.join(ROOT, "config", "keywords.yaml")))["section_headers"]
    rec_letter = ("Dear Members of the Admission Committee, My name is Professor Bae. "
                  "I have known Mr. Jun for three years as his research advisor. He is an "
                  "outstanding student and his work in his lab was excellent. He led his "
                  "team and his projects with great skill. I have seen him grow. I highly "
                  "recommend him for your program. Yours sincerely, Professor Bae. ") * 2
    sop = ("Personal Statement. My research interest is in power systems and control. "
           "I decided to pursue a master's degree because my goal is to study smart grids. "
           "I plan to research grid stability and my motivation is to advance the field. "
           "I came to this decision after my undergraduate projects. ") * 2
    labels = _viterbi([_page_scores(rec_letter, headers), _page_scores(sop, headers)])
    assert labels[0] == "lor"
    assert labels[1] == "sop"


def test_segmenter_detects_essay_style_sop():
    """An SOP that opens with a third-person hook (no 'I am/my goal' up front) is
    still detected via first-person density + SOP vocabulary, not mislabelled
    résumé."""
    from src.packet import _page_scores, _viterbi
    headers = __import__("yaml").safe_load(
        open(os.path.join(ROOT, "config", "keywords.yaml")))["section_headers"]
    essay_sop = ("With great power comes great electricity bill. More than a billion "
                 "people still lack access to reliable power across the world. ") + (
                 "I worked on smart-grid projects and I learned a great deal about control. "
                 "I want to pursue graduate study and my research interest is power systems. "
                 "My motivation drives me toward a doctoral career and admission to this program. ") * 4
    resume_like = ("Jane Roe  jane@example.com  +1 415 555 1234  EDUCATION  WORK EXPERIENCE  "
                   "PROJECTS  SKILLS: python, matlab, verilog ") * 3
    labels = _viterbi([_page_scores(resume_like, headers), _page_scores(essay_sop, headers)])
    assert labels[0] == "resume"
    assert labels[1] == "sop"


def test_empty_csv_rows_are_quarantined(tmp_path):
    """Blank / unmapped CSV rows (no cas_id, email, name, or data) are quarantined
    rather than surfaced as empty ROW-n applicants."""
    from src.store import Store, db_path_for
    csv = tmp_path / "rows.csv"
    csv.write_text("cas_id,first_name,last_name,ielts_overall_band_score_0\n"
                   "CAS900,Real,Person,7.0\n,,,\n,,,\n")
    field_map = os.path.join(ROOT, "config", "csv_field_map.yaml")
    summary = run(str(csv), str(tmp_path), field_map)
    assert summary["valid"] == 1 and summary["quarantined"] == 2
    store = Store(db_path_for(str(tmp_path)))
    assert [e["cas_id"] for e in store.index()] == ["CAS900"]
    assert all(c["cas_id"].startswith("ROW-") for c in store.quarantine())


def test_needs_ocr_catches_watermarked_scans():
    """Regression: a scanned page whose only embedded text is a watermark
    (e.g. 'Scanned by CamScanner', 21 chars) used to defeat the 20-char OCR
    threshold and be silently dropped. It must now be flagged for OCR."""
    from src.ocr import _needs_ocr
    assert _needs_ocr("Scanned by CamScanner", has_image=True) is True   # the bug case
    assert _needs_ocr("page 3", has_image=False) is True                 # near-empty
    dense = "word " * 200                                                # real text page
    assert _needs_ocr(dense, has_image=True) is False                    # don't OCR good text
    assert _needs_ocr(dense, has_image=False) is False


def test_page_has_image_detection():
    import io
    import pytest
    pytest.importorskip("reportlab")
    from PIL import Image
    from pypdf import PdfReader
    from src.ocr import _page_has_image
    # image-only PDF page -> detected as having an image
    im = Image.new("RGB", (200, 80), "white")
    buf = io.BytesIO(); im.save(buf, format="PDF")
    assert _page_has_image(PdfReader(io.BytesIO(buf.getvalue())).pages[0]) is True
    # a pure-text page -> no image
    from reportlab.pdfgen import canvas
    cb = io.BytesIO(); c = canvas.Canvas(cb)
    c.drawString(72, 720, "hello text only"); c.showPage(); c.save()
    assert _page_has_image(PdfReader(io.BytesIO(cb.getvalue())).pages[0]) is False


# --------------------------------------------------------------------------
# Web layer (Flask) — smoke tests through the test client
# --------------------------------------------------------------------------

def _client(tmp_path, monkeypatch):
    from src import app as appmod
    monkeypatch.setattr(appmod, "DATA", str(tmp_path))
    os.makedirs(os.path.join(str(tmp_path), "raw"), exist_ok=True)
    appmod.app.config.update(TESTING=True)
    return appmod, appmod.app.test_client()


def test_web_upload_browse_download_reset(tmp_path, monkeypatch):
    import io
    appmod, client = _client(tmp_path, monkeypatch)

    # empty state renders
    assert client.get("/").status_code == 200

    # upload the sample CSV through the form
    with open(CSV, "rb") as f:
        data = {"csv": (io.BytesIO(f.read()), "sample_applications.csv")}
    r = client.post("/upload", data=data, content_type="multipart/form-data",
                    follow_redirects=True)
    assert r.status_code == 200

    # the store now has students; the list + a detail page render
    from src.store import Store, db_path_for
    store = Store(db_path_for(str(tmp_path)))
    assert store.count() == 4
    cas = store.index()[0]["cas_id"]
    assert client.get(f"/student/{cas}").status_code == 200
    assert client.get("/student/NOPE").status_code == 404

    # summaries page + report download
    assert client.get("/summaries").status_code == 200
    dl = client.get("/download/summaries")
    assert dl.status_code == 200 and b"summaries" in dl.data.lower()

    # reset clears the store
    client.post("/reset", follow_redirects=True)
    assert Store(db_path_for(str(tmp_path))).count() == 0


def test_web_upload_rejects_empty(tmp_path, monkeypatch):
    appmod, client = _client(tmp_path, monkeypatch)
    r = client.post("/upload", data={}, content_type="multipart/form-data",
                    follow_redirects=True)
    assert r.status_code == 200          # flashes an error, doesn't crash


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
