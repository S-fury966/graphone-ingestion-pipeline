"""Anti-hallucination grounding: prompt + verbatim-evidence verification."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..common.logging import get_logger

log = get_logger("llm.grounding")

PROMPT_VERSION = "1"

SYSTEM_PROMPT = (
    "You are a precise information-extraction engine. Extract ONLY facts that are "
    "explicitly stated in the TEXT provided by the user. Never guess, infer, or use "
    "outside knowledge. If a field is not stated in the TEXT, set it to null.\n"
    "Return a SINGLE JSON object with exactly the schema fields, PLUS an \"_evidence\" "
    "object. For every field whose value is NOT null, \"_evidence\" must map that "
    "field name to a short verbatim quote (copied character-for-character from the "
    "TEXT) that supports the value. Do not include evidence for null fields.\n"
    "Output JSON only — no prose, no code fences."
)


def build_user_prompt(instructions: str, schema_hint: str, source_text: str,
                      repair_error: Optional[str] = None) -> str:
    parts = [
        "TASK:\n" + instructions.strip(),
        "SCHEMA FIELDS (extract these):\n" + schema_hint.strip(),
    ]
    if repair_error:
        parts.append("YOUR PREVIOUS OUTPUT WAS INVALID. Fix exactly this error and "
                     "return corrected JSON:\n" + repair_error.strip())
    parts.append("TEXT:\n\"\"\"\n" + source_text.strip() + "\n\"\"\"")
    return "\n\n".join(parts)


def schema_hint_from_model(schema_model: Any) -> str:
    """A compact field list from a Pydantic model for the prompt."""
    lines = []
    for fname, finfo in schema_model.model_fields.items():
        if fname == "_evidence":
            continue
        ann = getattr(finfo.annotation, "__name__", str(finfo.annotation))
        lines.append(f"- {fname} ({ann})")
    return "\n".join(lines)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def quote_grounded(quote: Optional[str], source_text: str) -> bool:
    """True if `quote` appears in `source_text` (whitespace-normalized, casefold)."""
    if not quote:
        return False
    return _normalize(str(quote)) in _normalize(source_text)


@dataclass
class GroundingResult:
    data: Dict[str, Any]
    evidence: Dict[str, str]
    nulled_fields: List[str] = field(default_factory=list)

    @property
    def nulled_count(self) -> int:
        return len(self.nulled_fields)


def verify_grounding(data: Dict[str, Any], source_text: str) -> GroundingResult:
    """Null any field whose evidence quote is not found verbatim in the source.

    A non-null field with no evidence, or evidence absent from the TEXT
    (whitespace-normalized, case-insensitive), is set to null. Numeric and enum
    fields are checked the same way.
    """
    data = dict(data)
    evidence_in = data.pop("_evidence", {}) or {}
    if not isinstance(evidence_in, dict):
        evidence_in = {}

    norm_source = _normalize(source_text)
    kept_evidence: Dict[str, str] = {}
    nulled: List[str] = []

    for fname, value in list(data.items()):
        if value is None:
            continue
        # Only ground scalar fields; lists/dicts (e.g. a products array with its
        # own inline evidence) are left to task-specific grounding.
        if isinstance(value, (list, dict)):
            continue
        quote = evidence_in.get(fname)
        ok = bool(quote) and _normalize(str(quote)) in norm_source
        if ok:
            kept_evidence[fname] = str(quote)
        else:
            data[fname] = None
            nulled.append(fname)
            log.info("grounding nulled field", extra={"extra": {
                "field": fname, "had_evidence": bool(quote)}})

    return GroundingResult(data=data, evidence=kept_evidence, nulled_fields=nulled)
