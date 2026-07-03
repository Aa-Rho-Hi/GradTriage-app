"""Stage 4 — validation via the strong Pydantic model.

Kept as a thin wrapper so the rest of the pipeline (and tests) can call a
simple (is_valid, errors) interface. The actual typing/coercion/range checks
live in src.models.StudentRecord.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .models import parse_record


def validate_record(record: dict, schema_path: Optional[str] = None
                    ) -> Tuple[bool, List[str]]:
    """Return (is_valid, [error messages]). schema_path is accepted for
    backwards compatibility but no longer used — the model is authoritative."""
    cleaned, errors = parse_record(record)
    return (cleaned is not None, errors)
