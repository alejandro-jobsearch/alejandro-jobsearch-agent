"""Web search through Exa, for postings no configured board carries: regional roles on company career sites or
on small boards. Results are web pages, not job records, so the title is split into role and company by pattern
and the location stays blank: the scorer reads who can apply from the text (`remote_eligibility`).

The index keeps pages after they die, so each result is checked with `linkedin.is_closed` before it counts.
"""
import logging
import re
import time
from datetime import date, timedelta
from urllib.parse import urlparse

import httpx

from jobagent import linkedin
from jobagent.models import Job

log = logging.getLogger(__name__)

SEARCH = "https://api.exa.ai/search"
TIMEOUT = httpx.Timeout(60)
MAX_TEXT = 8000
MIN_TEXT = 300

_PREFIX = re.compile(r"^\s*(\[[^\]]*\]\s*|remote\s+|hiring:?\s+)+", re.I)
_SITE = re.compile(r"\s+[|·]\s+[^|·]*$")                    # "Head of AI at Acme | SomeBoard"
_AT = re.compile(r"^(.+)\s+(?:at\s+|@\s*)([^@]+)$")         # greedy: the last "at" splits role from company
_EN = re.compile(r"^(.+)\s+en\s+((?:[A-Z][\w&.-]*\s?){1,3})$")   # only trusted on a board's title, see split_title
_DASH = re.compile(r"^(.+)\s+[-–—]\s+([^-–—]+)$")
_POSTING = re.compile(r"requisit|requirement|qualification|responsabilidad|responsibilit|funciones|what you", re.I)
_NOT_POSTING = re.compile(r"/(posts?|pulse|in|blog|news|reviews?|salaries|salarios)/", re.I)
_GENERIC = {"www", "careers", "career", "jobs", "job", "apply", "boards", "empleos", "trabajos"}
_ATS_PATH = re.compile(r"(ashbyhq|lever|greenhouse)\.(com|co|io)$")


def host_company(url: str) -> str:
    """Best guess at the company from the URL: `acme.teamtailor.com`, `jobs.lever.co/acme`, `careers.acme.com`."""
    u = urlparse(url)
    host = u.hostname or ""
    if _ATS_PATH.search(host) and (parts := [p for p in u.path.split("/") if p]):
        return parts[0].replace("-", " ").title()
    labels = [x for x in host.split(".") if x not in _GENERIC]
    return labels[0].replace("-", " ").title() if labels else ""


def split_title(raw: str, url: str) -> tuple[str, str]:
    """(role, company) from a page title. "Role at Company" wins; "Role - Company" only when the URL backs the
    company, since the part after a dash is as often a place; Spanish "Rol en Empresa" only when the title also
    names a board ("| SomeBoard") or the URL says "-at-", since "en" is usually part of the role. Falls back on
    the URL."""
    stripped = _PREFIX.sub("", raw or "")
    t = _SITE.sub("", stripped).strip()
    if m := _AT.match(t):
        return m[1].strip(), re.split(r"\s+[-–—]\s+", m[2])[0].strip()
    if (t != stripped.strip() or "-at-" in url) and (m := _EN.match(t)):
        return m[1].strip(), m[2].strip()
    if (m := _DASH.match(t)) and m[2].strip().lower().replace(" ", "") in url.lower():
        return m[1].strip(), m[2].strip()
    return t, host_company(url)


def to_job(r: dict, today: date, max_age_days: int, term: str) -> Job | None:
    """A Job from a search result, or None when the page is not a recent posting."""
    url, text = r.get("url") or "", (r.get("text") or "").strip()
    if not url or _NOT_POSTING.search(urlparse(url).path + "/") or len(text) < MIN_TEXT or not _POSTING.search(text):
        return None
    posted = None
    if r.get("publishedDate"):
        posted = date.fromisoformat(r["publishedDate"][:10])
        if posted < today - timedelta(days=max_age_days):
            return None
    title, company = split_title(r.get("title") or "", url)
    return Job(source="exa", company=company, title=title, url=url, description=text, date_posted=posted,
               search_term=term)


def search(cfg: dict, api_key: str, keep=None, sleep: float = 0.5) -> list[Job]:
    """Every query in `cfg["queries"]`; results that `keep(job)` accepts and whose page is still open."""
    today = date.today()
    jobs: dict[str, Job] = {}
    for q in cfg["queries"]:
        r = httpx.post(SEARCH, headers={"x-api-key": api_key}, timeout=TIMEOUT,
                       json={"query": q, "numResults": cfg.get("num_results", 15), "type": "auto",
                             "excludeDomains": cfg.get("exclude_domains", []),
                             "contents": {"text": {"maxCharacters": MAX_TEXT}}})
        r.raise_for_status()
        results = r.json().get("results", [])
        got = [j for x in results if (j := to_job(x, today, cfg.get("max_age_days", 45), q))]
        log.info("[exa] %r: %d results, %d postings", q, len(results), len(got))
        for j in got:
            if j.url not in jobs and (keep is None or keep(j)):
                jobs[j.url] = j
    out = []
    for j in jobs.values():
        if not linkedin.is_closed(j.url):
            out.append(j)
        time.sleep(sleep)
    log.info("[exa] %d postings, %d still open", len(jobs), len(out))
    return out
