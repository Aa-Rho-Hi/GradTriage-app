"""Regenerate student.schema.json from the Pydantic model (single source of truth).

    python -m scripts.gen_schema
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import StudentRecord

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    schema = StudentRecord.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$comment"] = "GENERATED from src/models.py — do not edit by hand."
    out = os.path.join(ROOT, "student.schema.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
