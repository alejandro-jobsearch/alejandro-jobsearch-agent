"""Public remote-job sources (no auth): Himalayas, We Work Remotely, Get on Board, Jobicy.

They publish worldwide, so the first filter is who can apply. Himalayas and Get on Board expose that as data;
We Work Remotely only as a region label. `Prefilter.eligible_ok` and the LLM's `remote_eligibility` do the rest.
"""
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

from jobagent.models import Job
from jobagent.sources.boards import TIMEOUT, _text

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "jobagent", "Accept": "application/json, application/xml"}


def _epoch(v) -> date | None:
    return datetime.fromtimestamp(int(v), tz=timezone.utc).date() if v else None


def _fresh(j: Job, max_age_days: int) -> bool:
    return j.date_posted is None or j.date_posted >= date.today() - timedelta(days=max_age_days)


def himalayas(terms: list[str], country: str, utc_offset: float | None, sleep: float = 1.0) -> list[Job]:
    """Postings open to `country` or worldwide. `utc_offset` drops those limited to time zones that exclude it."""
    out, seen = [], set()
    for term in terms:
        r = httpx.get("https://himalayas.app/jobs/api/search", headers=HEADERS, timeout=TIMEOUT,
                      params={"q": term, "country": country, "worldwide": "true"})
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            url = j.get("guid") or j.get("applicationLink")
            zones = j.get("timezoneRestrictions") or []
            if not url or url in seen or (zones and utc_offset is not None and utc_offset not in zones):
                continue
            seen.add(url)
            places = [p["name"] if isinstance(p, dict) else str(p) for p in j.get("locationRestrictions") or []]
            out.append(Job(source="himalayas", company=j.get("companyName") or "", title=j.get("title") or "",
                           location=", ".join(places) or "Worldwide", url=url, remote=True, search_term=term,
                           description=_text(j.get("description") or j.get("excerpt") or ""),
                           date_posted=_epoch(j.get("pubDate")), salary_min=j.get("minSalary"),
                           salary_max=j.get("maxSalary"), currency=j.get("currency")))
        time.sleep(sleep)
    return out


def weworkremotely(url: str = "https://weworkremotely.com/remote-jobs.rss") -> list[Job]:
    r = httpx.get(url, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    out = []
    for it in ET.fromstring(r.content).iter("item"):
        company, _, title = (it.findtext("title") or "").partition(":")
        when = it.findtext("pubDate")
        out.append(Job(source="weworkremotely", company=company.strip(), title=title.strip(),
                       location=(it.findtext("region") or "").strip(), url=(it.findtext("link") or "").strip(),
                       description=_text(it.findtext("description") or ""), remote=True,
                       date_posted=parsedate_to_datetime(when).date() if when else None))
    return [j for j in out if j.url and j.title]


def getonboard(terms: list[str], country: str, sleep: float = 1.0) -> list[Job]:
    """Keeps fully remote postings, and remote_local ones that list `country`. Salaries are USD per month."""
    out, seen = [], set()
    for term in terms:
        r = httpx.get("https://www.getonbrd.com/api/v0/search/jobs", headers=HEADERS, timeout=TIMEOUT,
                      params={"query": term, "per_page": 50, "expand": '["company"]'})
        r.raise_for_status()
        for j in r.json().get("data", []):
            a = j["attributes"]
            countries = [str(c) for c in a.get("countries") or []]
            local = any(re.search(country, c, re.I) for c in countries)
            modality = a.get("remote_modality")
            if j["id"] in seen or not a.get("remote") or not (modality == "fully_remote" or local):
                continue
            seen.add(j["id"])
            body = "\n".join(filter(None, (_text(a.get(k) or "") for k in
                                           ("projects", "functions", "description", "desirable", "benefits"))))
            company = ((a.get("company") or {}).get("data") or {}).get("attributes", {}).get("name", "")
            out.append(Job(source="getonboard", company=html.unescape(company), title=html.unescape(a.get("title") or ""),
                           location=country if local else "Remote - Latin America", remote=True, description=body,
                           url=(j.get("links") or {}).get("public_url") or f"https://www.getonbrd.com/jobs/{j['id']}",
                           date_posted=_epoch(a.get("published_at")), salary_min=a.get("min_salary"),
                           salary_max=a.get("max_salary"), currency="USD" if a.get("max_salary") else None,
                           search_term=term))
        time.sleep(sleep)
    return out


def jobicy(geo: str = "latam", count: int = 50) -> list[Job]:
    """Jobicy API, newest postings for a region. Its tag search matches loosely, so the prefilter picks by title."""
    r = httpx.get("https://jobicy.com/api/v2/remote-jobs", headers=HEADERS, timeout=TIMEOUT,
                  params={"count": count, "geo": geo})
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        when = (j.get("pubDate") or "")[:10]
        out.append(Job(source="jobicy", company=html.unescape(j.get("companyName") or ""),
                       title=html.unescape(j.get("jobTitle") or ""), location=j.get("jobGeo") or "",
                       url=j.get("url") or "", description=_text(j.get("jobDescription") or ""), remote=True,
                       date_posted=date.fromisoformat(when) if when else None))
    return [j for j in out if j.url and j.title]


def search(cfg: dict) -> list[Job]:
    """Each source fails alone. `cfg`: terms, country, utc_offset, max_age_days, sources."""
    terms, country = cfg["terms"], cfg.get("country", "Peru")
    calls = {"himalayas": lambda: himalayas(terms, country, cfg.get("utc_offset")),
             "weworkremotely": weworkremotely,
             "getonboard": lambda: getonboard(terms, cfg.get("country_pattern", country)),
             "jobicy": lambda: jobicy(cfg.get("jobicy_geo", "latam"))}
    out = []
    for name in cfg.get("sources", list(calls)):
        try:
            got = [j for j in calls[name]() if _fresh(j, cfg.get("max_age_days", 14))]
            log.info("[remote/%s] %d", name, len(got))
            out += got
        except Exception as e:  # a blocked or changed source must not sink the others
            log.warning("[remote/%s] %s", name, e)
    return out
