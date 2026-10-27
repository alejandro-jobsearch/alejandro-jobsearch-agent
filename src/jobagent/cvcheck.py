"""Pre-send checks for a tailored CV: claims the profile does not back, expired certifications, the posting's
title as headline, and sentences lifted from the posting. Errors block `postular publish`.

Data comes from the private profile, so nothing personal lives here:
- credentials.yml: `status: expired` entries, optional `cv_aliases` (how CVs tend to write them, e.g. "ITIL v4").
  An expired one may appear only followed by its years, e.g. "Scrum Master Certified (2021–2024)".
- experience.yml: optional top-level `forbidden_claims: [{pattern, why}]` checked on the whole CV, and per role
  `forbid: [{pattern, why}]` checked on that role's bullets in the plan (regex, case/accent-insensitive).
"""
import re
import unicodedata

from rapidfuzz import fuzz

from jobagent import ats
from jobagent.models import _norm

YEARS = re.compile(r"(19|20)\d\d\s*[-–—]\s*(19|20)\d\d")
MIRROR_MAX = 4          # % of the whole CV inside 5-word runs copied from the posting
MIRROR_TOP_MAX = 6      # same, for headline + summary + current role (what a screener reads first)
TITLE_SIMILAR = 90


def _plain(s: str) -> str:
    """Lowercase, no accents, punctuation kept (so year ranges and parentheses survive)."""
    s = re.sub(r"[‐-―−]", "-", s or "")              # en/em dashes would be dropped by the ascii pass
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def _dated(tail: str) -> bool:
    """True when the mention is followed by its validity years before the next list separator."""
    return bool(YEARS.search(re.split(r"[,;|\n•]", tail)[0]))


def expired_certs(text: str, credentials: list[dict]) -> list[str]:
    out, t = [], _plain(text)
    for c in (c for c in credentials if c.get("status") == "expired"):
        bad = next((a for a in [c["name"], *c.get("cv_aliases", [])]
                    for m in re.finditer(rf"(?<![a-z0-9]){re.escape(_plain(a))}(?![a-z0-9])", t)
                    if not _dated(t[m.end():m.end() + 45])), None)
        if bad:
            out.append(f"certificación vencida sin años: «{bad}» ({c['name']}, venció {c.get('expires')})")
    return out


def _claims(text: str, rules: list[dict], where: str) -> list[str]:
    t = _plain(text)
    return [f"{where}: «{m.group(0)}» — {r.get('why', r['pattern'])}"
            for r in rules for m in [re.search(r["pattern"], t)] if m]


def headline(head: str, posting_title: str, held_titles: list[str]) -> list[str]:
    """The headline may not present the posting's title as the candidate's own unless he held it."""
    first, title = _norm(re.split(r"[|·•]", head or "")[0]), _norm(posting_title)
    if not first or not title:
        return []
    if fuzz.partial_ratio(title, first) < TITLE_SIMILAR and fuzz.partial_ratio(first, title) < TITLE_SIMILAR:
        return []
    if any(fuzz.token_set_ratio(title, _norm(h)) >= TITLE_SIMILAR for h in held_titles):
        return []
    return [f"titular usa el cargo de la vacante («{posting_title}») como si fuera propio: usa tu cargo real + dominio"]


def check(plan: dict, cv_text: str, job: dict, experience: dict, credentials: list[dict]) -> dict:
    """{'errors': [...], 'mirror': %, 'mirror_top': %, 'mirror_runs': [...]} for a built CV."""
    roles = experience.get("roles", [])
    held = [t for r in roles for t in (r.get("title") or {}).values()]
    errors = headline(plan.get("headline", ""), job.get("title", ""), held)
    errors += expired_certs(cv_text, credentials)
    errors += _claims(cv_text, experience.get("forbidden_claims", []), "CV")
    by_title = {t: r for r in roles for t in (r.get("title") or {}).values()}
    for title, bullets in (plan.get("roles") or {}).items():
        if rules := (by_title.get(title) or {}).get("forbid"):
            errors += _claims("\n".join(bullets), rules, title)

    posting = job.get("description", "")
    pct, runs = ats.mirror(cv_text, posting)
    current = next(iter((plan.get("roles") or {}).values()), [])
    top_pct, top_runs = ats.mirror("\n".join([plan.get("headline", ""), plan.get("summary", ""), *current]), posting)
    if posting and pct > MIRROR_MAX:
        errors.append(f"espejo: {pct}% del CV son frases copiadas de la vacante (máx {MIRROR_MAX}%)")
    if posting and top_pct > MIRROR_TOP_MAX:
        errors.append(f"espejo arriba: {top_pct}% de titular+resumen+rol actual copiado (máx {MIRROR_TOP_MAX}%): "
                      + "; ".join(f"«{r}»" for r in top_runs[:5]))
    return {"errors": errors, "mirror": pct, "mirror_top": top_pct, "mirror_runs": runs[:15]}
