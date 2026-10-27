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


MIRROR_N = 5


def mirror(cv_text: str, posting: str, n: int = MIRROR_N) -> tuple[int, list[str]]:
    """% of CV words that sit inside a run of >= n consecutive words copied from the posting, plus those runs.

    Keyword coverage rewards using the posting's vocabulary; this measures the opposite failure — sentences
    lifted from the posting, which a human screener spots at once."""
    cv, jd = _norm(cv_text).split(), _norm(posting).split()
    grams = {tuple(jd[i:i + n]) for i in range(len(jd) - n + 1)}
    covered: set[int] = set()
    for i in range(len(cv) - n + 1):
        if tuple(cv[i:i + n]) in grams:
            covered.update(range(i, i + n))
    runs, cur = [], []
    for i in sorted(covered):
        if cur and i != cur[-1] + 1:
            runs.append(cur)
            cur = []
        cur.append(i)
    if cur:
        runs.append(cur)
    return (round(100 * len(covered) / len(cv)) if cv else 0), [" ".join(cv[r[0]:r[-1] + 1]) for r in runs]
