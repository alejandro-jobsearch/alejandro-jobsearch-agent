"""Web search through Exa, for postings no configured board carries: regional roles on company career sites or
on small boards. Results are web pages, not job records, so the title is split into role and company by pattern
and the location stays blank (Computrabajo titles carry it): the scorer reads who can apply from the text
(`remote_eligibility`).

The index keeps pages after they die, so each result is checked with `is_closed` before it counts: the page
itself, and the ATS apply links it carries (a careers page can outlive the posting it links to).
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
_SITE = re.compile(r"\s+[|·]\s+")      # "Head of AI at Acme | SomeBoard", "Rol en Acme · Empleos remotos · heyeco"
_REMOTE_JOB = re.compile(r"\s+[-–—]\s+remote(\s+job)?\s*$", re.I)   # aggregator suffix on the role
_AT = re.compile(r"^(.+)\s+(?:at\s+|@\s*)([^@]+)$")         # greedy: the last "at" splits role from company
_EN = re.compile(r"^(.+)\s+en\s+((?:[A-Z0-9][\w&.-]*\s?){1,3})$")   # only trusted on a board's title, see split_title
_DASH = re.compile(r"^(.+)\s+[-–—]\s+([^-–—]+)$")
_POSTING = re.compile(r"requisit|requerimiento|requirement|qualification|responsabilidad|responsibilit|funciones|what you", re.I)
_NOT_POSTING = re.compile(r"/(posts?|pulse|in|blog|news|reviews?|salaries|salarios)/", re.I)
# Computrabajo: "Empleo deGerente de TI en Acme S.A.C. - Miraflores" (the space after "de" is lost); its list
# pages (/trabajo-de-...) are not postings. The site blocks robots, so is_closed can't tell and keeps them.
_CT_TITLE = re.compile(r"^Empleo de\s*(.+) en (.+?) - ([^-]+)$")
_CT_POSTING = re.compile(r"/ofertas-de-trabajo/oferta-de-trabajo-")
_GENERIC = {"www", "careers", "career", "jobs", "job", "apply", "boards", "empleos", "trabajos"}
_ATS_PATH = re.compile(r"(ashbyhq|lever|greenhouse)\.(com|co|io)$")
# Apply links on a company's own careers page or on an aggregator's copy. Only ATSs that answer 404/410 for a
# removed posting: InHire and Ashby render their "not found" page client-side with HTTP 200, so a request can't
# tell. Teamtailor answers 410 on its own domain (checked 2026-11-09, knowmad mood #87); custom domains not tested.
_APPLY = re.compile(r'https://(?:jobs\.lever\.co|(?:boards|job-boards)\.greenhouse\.io|[\w-]+\.factorial\.(?:mx|co|es|com)'
                    r'|apply\.workable\.com|[\w-]+(?:\.na)?\.teamtailor\.com)/[^"\'\s<>\\]+')


def apply_links(page_html: str, page_url: str) -> list[str]:
    """ATS apply links found in a careers page, excluding the page itself (at most 3)."""
    seen: list[str] = []
    for u in _APPLY.findall(page_html or ""):
        u = u.rstrip(").,;")
        if u.split("?")[0] != page_url.split("?")[0] and u not in seen:
            seen.append(u)
    return seen[:3]


_TT_JOB = re.compile(r"(https://[\w.-]+\.teamtailor\.com/jobs/\d+[\w-]*)")
_RESIDE = re.compile(r"¿\s*(?:resides|vives|resid[ií]s)\s+en\s+([A-ZÁÉÍÓÚ][\wáéíóúñ ]{2,30}?)\s*\?", re.I)


def residency_required(url: str) -> str | None:
    """The country a Teamtailor application form demands residence in, when it is not Peru. The posting can say
    "Completamente remoto" while the form asks "¿Resides en Chile?" as a knockout (Tenpo #94, 2026-11-09)."""
    m = _TT_JOB.match(url or "")
    if not m:
        return None
    r = linkedin._get(m[1] + "/applications/new")
    if r is None or r.status_code != 200:
        return None
    place = _RESIDE.search(r.text)
    return place[1].strip() if place and not re.match(r"per[uú]", place[1].strip(), re.I) else None


def is_closed(url: str) -> bool | None:
    """The page is gone, or it links to an ATS posting that is gone (a careers page often stays up after the
    posting closes). None = could not tell; never drop on a failed request."""
    r = linkedin._get(url)
    if r is None:
        return None
    if r.status_code in (404, 410) or "error=true" in str(r.url):
        return True
    if r.status_code != 200:
        return None
    for link in apply_links(r.text, url):
        if linkedin.is_closed(link):
            log.info("[exa] apply link closed: %s → %s", url, link)
            return True
    return False


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
    t = _SITE.split(stripped)[0].strip()   # everything after the first separator is the board, not the posting
    if m := _AT.match(t):
        return _REMOTE_JOB.sub("", m[1]).strip(), re.split(r"\s+[-–—]\s+", m[2])[0].strip()
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
    location = ""
    if "computrabajo." in url:
        if not _CT_POSTING.search(url) or not (m := _CT_TITLE.match((r.get("title") or "").strip())):
            return None
        title, company, location = m[1].strip(), m[2].strip(), f"{m[3].strip()}, Perú"
    else:
        title, company = split_title(r.get("title") or "", url)
    return Job(source="exa", company=company, title=title, location=location, url=url, description=text,
               date_posted=posted, search_term=term)


def query_body(entry: str | dict, cfg: dict) -> tuple[str, dict]:
    """A query is a string, or {q, domains} to search only those sites (small ATSs, boards that block robots like
    Computrabajo, multilaterals). With `domains` the global `exclude_domains` does not apply: Exa takes one or
    the other."""
    q, domains = (entry, None) if isinstance(entry, str) else (entry["q"], entry.get("domains"))
    body = {"query": q, "numResults": cfg.get("num_results", 15), "type": "auto",
            "contents": {"text": {"maxCharacters": MAX_TEXT}}}
    if domains:
        body["includeDomains"] = domains
    else:
        body["excludeDomains"] = cfg.get("exclude_domains", [])
    return q, body


def search(cfg: dict, api_key: str, keep=None, sleep: float = 0.5) -> list[Job]:
    """Every query in `cfg["queries"]`; results that `keep(job)` accepts and whose page is still open."""
    today = date.today()
    jobs: dict[str, Job] = {}
    for entry in cfg["queries"]:
        q, body = query_body(entry, cfg)
        r = httpx.post(SEARCH, headers={"x-api-key": api_key}, timeout=TIMEOUT, json=body)
        r.raise_for_status()
        results = r.json().get("results", [])
        got = [j for x in results if (j := to_job(x, today, cfg.get("max_age_days", 45), q))]
        log.info("[exa] %r: %d results, %d postings", q, len(results), len(got))
        for j in got:
            if j.url not in jobs and (keep is None or keep(j)):
                jobs[j.url] = j
    out = []
    for j in jobs.values():
        if not is_closed(j.url):
            if place := residency_required(j.url):
                log.info("[exa] residency required (%s): %s", place, j.url)
            else:
                out.append(j)
        time.sleep(sleep)
    log.info("[exa] %d postings, %d still open", len(jobs), len(out))
    return out
