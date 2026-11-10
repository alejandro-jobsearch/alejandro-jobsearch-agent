"""Public job-board APIs and feeds (no auth): Greenhouse, Lever, Ashby, HireHive, Workday, SuccessFactors RSS,
amazon.jobs and Michael Page."""
import html
import json
import logging
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from jobagent.models import Job

log = logging.getLogger(__name__)
TIMEOUT = httpx.Timeout(30)


def _text(fragment: str) -> str:
    s = html.unescape(fragment or "")
    s = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", s)
    s = re.sub(r"<li[^>]*>", "- ", s)
    s = re.sub(r"<[^>]+>", "", html.unescape(s))
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def _date(v) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):  # Lever: epoch millis
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc).date()
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()


def greenhouse(slug: str, company: str) -> list[Job]:
    r = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                  params={"content": "true"}, timeout=TIMEOUT)
    r.raise_for_status()
    return [Job(source="greenhouse", company=company, title=j["title"],
                location=(j.get("location") or {}).get("name", ""), url=j["absolute_url"],
                description=_text(j.get("content", "")), date_posted=_date(j.get("updated_at")))
            for j in r.json()["jobs"]]


def lever(slug: str, company: str) -> list[Job]:
    r = httpx.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location", "")]
        desc = "\n".join([j.get("descriptionPlain", "")] + [_text(l.get("content", "")) for l in j.get("lists", [])])
        out.append(Job(source="lever", company=company, title=j["text"], location=" | ".join(filter(None, locs)),
                       url=j["hostedUrl"], description=desc, date_posted=_date(j.get("createdAt")),
                       remote=(j.get("workplaceType") == "remote") or None))
    return out


def ashby(slug: str, company: str) -> list[Job]:
    r = httpx.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                  params={"includeCompensation": "true"}, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json()["jobs"]:
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations", [])]
        out.append(Job(source="ashby", company=company, title=j["title"], location=" | ".join(filter(None, locs)),
                       url=j["jobUrl"], description=j.get("descriptionPlain") or _text(j.get("descriptionHtml", "")),
                       date_posted=_date(j.get("publishedAt")), remote=j.get("isRemote")))
    return out


def hirehive(slug: str, company: str) -> list[Job]:
    url, out = f"https://{slug}.hirehive.com/api/v1/jobs", []
    while url:  # 30 per page, `nextPage` is the absolute URL of the next one
        r = httpx.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        for j in d["jobs"]:
            country = (j.get("country") or {}).get("name", "")
            ts = j.get("publishedDate")  # epoch seconds (Lever's are millis)
            out.append(Job(source="hirehive", company=company, title=j["title"],
                           location=", ".join(filter(None, [j.get("location"), country])), url=j["hostedUrl"],
                           description=_text((j.get("description") or {}).get("html", "")),
                           date_posted=datetime.fromtimestamp(int(ts), tz=timezone.utc).date() if ts else None))
        url = d.get("nextPage")
    return out


def _country_facets(facets: list[dict], pattern: str) -> dict[str, list[str]]:
    """{facetParameter: [ids]} of the country facet values matching `pattern` (Workday nests them in groups)."""
    out: dict[str, list[str]] = {}
    for f in facets:
        vals = f.get("values") or []
        if re.search("country", f.get("facetParameter") or "", re.I):
            ids = [v["id"] for v in vals if v.get("id") and re.search(pattern, v.get("descriptor") or "", re.I)]
            if ids:
                out.setdefault(f["facetParameter"], []).extend(ids)
        for k, v in _country_facets([v for v in vals if "values" in v], pattern).items():
            out.setdefault(k, []).extend(v)
    return out


def workday(slug: str, company: str, country: str | None = None, search: list[str] | None = None, keep=None,
            max_per_search: int = 200, sleep: float = 0.5) -> list[Job]:
    """Workday career site, `slug` = "tenant.wdN/site" (from the URL tenant.wdN.myworkdayjobs.com/en-US/site).
    `country` (regex) picks the site's country facet: its full-text search also matches descriptions, so
    "Peru" there brings postings from anywhere. No matching facet = the company is not hiring there now.
    The list only has title and place, so the description is fetched only for postings `keep(job)` accepts."""
    host, site = slug.split("/", 1)
    tenant = host.split(".")[0]
    base = f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    facets: dict[str, list[str]] = {}
    if country:
        r = httpx.post(f"{base}/jobs", json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                       timeout=TIMEOUT)
        r.raise_for_status()
        facets = _country_facets(r.json().get("facets", []), country)
        if not facets:
            log.info("[workday/%s] no openings in %s", tenant, country)
            return []
    found: dict[str, dict] = {}
    for text in search or [""]:
        offset = 0
        while offset < max_per_search:
            r = httpx.post(f"{base}/jobs", json={"appliedFacets": facets, "limit": 20, "offset": offset,
                                                  "searchText": text}, timeout=TIMEOUT)
            r.raise_for_status()
            page = r.json().get("jobPostings", [])
            for p in page:
                found.setdefault(p["externalPath"], p)
            if len(page) < 20:
                break
            offset += 20
    out = []
    for path, p in found.items():
        j = Job(source="workday", company=company, title=p.get("title", ""), location=p.get("locationsText", ""),
                url=f"https://{host}.myworkdayjobs.com/{site}{path}")
        if keep and not keep(j):
            continue
        try:
            r = httpx.get(base + path, timeout=TIMEOUT)
            r.raise_for_status()
            d = r.json().get("jobPostingInfo", {})
        except Exception as e:  # one broken posting must not sink the board
            log.warning("[workday/%s] %s: %s", tenant, path, e)
            continue
        places = [d.get("location", "")] + list(d.get("additionalLocations") or [])
        country = (d.get("country") or {}).get("descriptor", "")
        out.append(j.model_copy(update={
            "location": ", ".join(filter(None, places + [country])) or j.location,
            "url": d.get("externalUrl") or j.url, "description": _text(d.get("jobDescription", "")),
            "date_posted": _date(d.get("startDate")),
            "remote": True if re.search(r"remot", d.get("remoteType") or "", re.I) else None}))
        time.sleep(sleep)
    return out


_PLACE = re.compile(r"\s*\(([^()]*)\)\s*$")


def successfactors(slug: str, company: str, search: list[str] | None = None, keep=None, locale: str = "es_ES",
                   place: str | None = None) -> list[Job]:
    """SAP SuccessFactors career site (jobs.scotiabank.com, jobs.iadb.org): its RSS feed per keyword, at most 20
    postings each, newest first. `slug` is the host. The place comes at the end of the title, "Role (Lima, LIM,
    PE)"; `place` ("LIM") is ANDed to each keyword, since the feed has no location parameter."""
    found: dict[str, Job] = {}
    for text in search or [""]:
        keywords = f"({text}) AND ({place})" if place and text else (place or text)
        r = httpx.get(f"https://{slug}/services/rss/job/", params={"locale": locale, "keywords": keywords},
                      timeout=TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        for it in ET.fromstring(r.content).iter("item"):
            raw, link = it.findtext("title") or "", (it.findtext("link") or "").split("?")[0]
            m = _PLACE.search(raw)
            when = it.findtext("pubDate")
            j = Job(source="successfactors", company=company, title=_PLACE.sub("", raw).strip(),
                    location=m[1] if m else "", url=link, description=_text(it.findtext("description") or ""),
                    date_posted=parsedate_to_datetime(when).date() if when else None, search_term=text or None)
            if link and link not in found and (keep is None or keep(j)):
                found[link] = j
    return list(found.values())


def amazon(slug: str, company: str, keep=None, limit: int = 100) -> list[Job]:
    """amazon.jobs (AWS included). `slug` = ISO-3 country code, e.g. PER."""
    r = httpx.get("https://www.amazon.jobs/en/search.json", timeout=TIMEOUT,
                  headers={"Accept-Encoding": "identity"},   # its gzip stream breaks httpx's decoder
                  params={"normalized_country_code[]": slug, "result_limit": limit, "sort": "recent"})
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        desc = "\n".join(_text(j.get(k) or "") for k in ("description", "basic_qualifications",
                                                          "preferred_qualifications"))
        job = Job(source="amazon", company=j.get("company_name") or company, title=j.get("title", ""),
                  location=j.get("normalized_location") or j.get("location", ""),
                  url="https://www.amazon.jobs" + j.get("job_path", ""), description=desc,
                  date_posted=datetime.strptime(j["posted_date"], "%B %d, %Y").date() if j.get("posted_date") else None)
        if keep is None or keep(job):
            out.append(job)
    return out


_LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def michaelpage(slug: str, company: str, keep=None, sleep: float = 1.0) -> list[Job]:
    """Michael Page job list page (`slug` = host + path, e.g. www.michaelpage.pe/jobs/tecnología). Each posting
    page carries a schema.org JobPosting; the client is confidential, so the company stays the recruiter."""
    host = slug.split("/")[0]
    r = httpx.get(f"https://{slug}", timeout=TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    out = []
    for path in dict.fromkeys(re.findall(r'href="(/job-detail/[^"]+)"', r.text)):
        url = f"https://{host}{path}"
        title = urllib.parse.unquote(path.split("/")[2]).replace("-", " ")
        if keep and not keep(Job(source="michaelpage", company=company, title=title, url=url)):
            continue
        try:
            page = httpx.get(url, timeout=TIMEOUT, follow_redirects=True)
            page.raise_for_status()
            d = next(x for x in (json.loads(m, strict=False) for m in _LD.findall(page.text))
                     if x.get("@type") == "JobPosting")
        except Exception as e:
            log.warning("[michaelpage] %s: %s", url, e)
            continue
        addr = ((d.get("jobLocation") or {}).get("address") or {}) if isinstance(d.get("jobLocation"), dict) else {}
        place = ", ".join(filter(None, [addr.get("addressLocality"), addr.get("addressRegion"),
                                         addr.get("addressCountry")]))
        out.append(Job(source="michaelpage", company=company, title=html.unescape(d.get("title") or title),
                       location=place, url=url, description=_text(d.get("description", "")),
                       date_posted=_date(d.get("datePosted")), job_type=_EMPLOYMENT.get(d.get("employmentType"))))
        time.sleep(sleep)
    return out


_EMPLOYMENT = {"FULL_TIME": "fulltime", "PART_TIME": "parttime", "TEMPORARY": "temporary", "CONTRACTOR": "contract",
               "INTERN": "internship"}

PROVIDERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "hirehive": hirehive, "workday": workday,
             "successfactors": successfactors, "amazon": amazon, "michaelpage": michaelpage}
# providers that read a list first and can skip fetching postings whose title the prefilter would drop
FILTERED = {"workday", "successfactors", "amazon", "michaelpage"}
