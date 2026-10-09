"""Deterministic ATS keyword coverage.

The LLM extracts the posting's keywords (with aliases) once; coverage against a CV is then plain
string matching, so the same number is reproducible for the base CV ("before") and the tailored
CV ("after"). Keywords get reformulated in the CV, never fabricated.

A keyword counts as present if the exact phrase (or an alias) appears, or if most of its
meaningful words appear anywhere in the CV (prefix match, so "arquitectura"~"arquitecto",
"governance"~"govern").
"""
import re

from jobagent.models import _norm

WEIGHT = {"must": 2, "nice": 1}
STOP = set("""a an and the of for in on with to by at or as de del la las el los y e en con para por al
un una o u que se su sus""".split())
PREFIX = 5
MIN_TOKEN_SHARE = 0.75


def _tokens(text: str) -> list[str]:
    return [t for t in _norm(text).split() if t not in STOP and len(t) > 1]


def _prefixes(text: str) -> set[str]:
    return {t[:PREFIX] for t in _tokens(text)}


def _has(cv_norm: str, cv_prefixes: set[str], phrase: str) -> bool:
    p = _norm(phrase)
    if p and re.search(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])", cv_norm):
        return True
    toks = _tokens(phrase)
    if len(toks) < 2:                       # single words must match exactly (above)
        return False
    return sum(t[:PREFIX] in cv_prefixes for t in toks) / len(toks) >= MIN_TOKEN_SHARE


def coverage(keywords: list[dict], cv_text: str) -> tuple[int, list[str]]:
    """Weighted % of keywords (term or any alias) present in the CV, plus the missing terms."""
    cv_norm, cv_prefixes = _norm(cv_text), _prefixes(cv_text)
    total = got = 0
    missing = []
    for k in keywords:
        w = WEIGHT.get(k.get("importance"), 1)
        total += w
        if any(_has(cv_norm, cv_prefixes, t) for t in [k["term"], *k.get("aliases", [])]):
            got += w
        else:
            missing.append(k["term"])
    return (round(100 * got / total) if total else 0), missing
