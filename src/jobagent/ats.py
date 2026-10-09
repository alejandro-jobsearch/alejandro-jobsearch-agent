"""Deterministic ATS keyword coverage.

The LLM extracts the posting's keywords (with aliases) once; coverage against a CV is then plain
string matching, so the same number is reproducible for the base CV ("before") and the tailored
CV ("after"). Keywords get reformulated in the CV, never fabricated.
"""
import re

from jobagent.models import _norm

WEIGHT = {"must": 2, "nice": 1}


def _has(text: str, phrase: str) -> bool:
    p = _norm(phrase)
    return bool(p) and re.search(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])", text) is not None


def coverage(keywords: list[dict], cv_text: str) -> tuple[int, list[str]]:
    """Weighted % of keywords (term or any alias) present in the CV, plus the missing terms."""
    text = _norm(cv_text)
    total = got = 0
    missing = []
    for k in keywords:
        w = WEIGHT.get(k.get("importance"), 1)
        total += w
        if any(_has(text, t) for t in [k["term"], *k.get("aliases", [])]):
            got += w
        else:
            missing.append(k["term"])
    return (round(100 * got / total) if total else 0), missing
