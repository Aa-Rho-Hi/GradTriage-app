"""Graduate applicant summary pipeline (local, deterministic; CSV/Excel + packets -> per-student JSON -> summary)."""
import logging as _logging

# pypdf logs noisy WARNING messages ("Impossible to decode XFormObject ...") for
# embedded images/forms in real PDFs. They're harmless — the text layer still
# extracts — so keep only real errors.
_logging.getLogger("pypdf").setLevel(_logging.ERROR)

SCHEMA_VERSION = "2.0.0"
