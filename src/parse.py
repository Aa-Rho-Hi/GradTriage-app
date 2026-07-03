"""Stage 3 — deterministic extraction.

Reads the CSV and collapses indexed columns (name_0, name_1, ...) into a
structured "grouped" view per row. No interpretation, no LLM.
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Tuple

import pandas as pd

_INDEXED = re.compile(r"^(.*)_(\d+)$")
_EXCEL_EXT = {".xlsx", ".xls", ".xlsm", ".xlsb", ".ods"}


def _cell(v) -> str:
    """Render a cell as clean text. Excel stores numbers as floats, so an
    integer like 1001 comes back as 1001.0 — strip the trailing .0."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def read_rows(path: str) -> List[Dict[str, str]]:
    """Read a CSV **or Excel** file as a list of {column: value} dicts.

    Uses pandas for robust handling of quoting, encodings, and the .xlsx/.xls
    formats. Everything is returned as text; typing happens later in the
    Pydantic model layer.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in _EXCEL_EXT:
        df = pd.read_excel(path, dtype=object)          # infer, then clean per-cell
    else:
        df = pd.read_csv(path, dtype=str, keep_default_na=False,
                         na_filter=False, encoding="utf-8-sig", skip_blank_lines=True)
    df.columns = [str(c).strip() for c in df.columns]
    return [{k: _cell(v) for k, v in rec.items()} for rec in df.to_dict("records")]


def group_indexed(row: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, Dict[int, str]]]:
    """Split a row into (scalars, indexed_groups).

    scalars: columns with no trailing _<n>.
    indexed_groups: base_name -> {index -> value} for columns ending in _<n>.
    """
    scalars: Dict[str, str] = {}
    groups: Dict[str, Dict[int, str]] = {}
    for col, val in row.items():
        m = _INDEXED.match(col)
        if m:
            base, idx = m.group(1), int(m.group(2))
            groups.setdefault(base, {})[idx] = val
        else:
            scalars[col] = val
    return scalars, groups
