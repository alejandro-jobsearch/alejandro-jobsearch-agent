"""LinkedIn guest pages (no login): is a posting still open, and how big/where is the company.

Also a generic `is_closed` for ATS URLs (Greenhouse/Lever answer 404 or redirect with ?error=true once a
posting is taken down). Any network error means "unknown" (None): never close or drop on a failed request.
"""
import html
import logging
import re
import time

import httpx

log = logging.getLogger(__name__)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}
JOB_ID = re.compile(r"linkedin\.com/jobs/view/(?:[^/?]*-)?(\d+)")


def _get(url: str) -> httpx.Response | None:
    for attempt in range(2):
        try:
            r = httpx.get(url, headers=UA, timeout=20, follow_redirects=True)
            if r.status_code != 429:
                return r
        except httpx.HTTPError as e:
            log.warning("GET %s: %s", url, e)
            return None
        time.sleep(5 * (attempt + 1))
    return None


def is_closed(url: str) -> bool | None:
    """True = no longer accepting applications, False = open, None = could not tell."""
    if m := JOB_ID.search(url):
        r = _get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m.group(1)}")
        if r is None:
            return None
        if r.status_code in (404, 410):
            return True
        if r.status_code != 200:
            return None
        return "closed-job__flavor--closed" in r.text or "No longer accepting applications" in r.text
    r = _get(url)
    if r is None:
        return None
    if r.status_code in (404, 410) or "error=true" in str(r.url):
        return True
    return False if r.status_code == 200 else None


def _dd(text: str, test_id: str) -> str | None:
    m = re.search(rf'data-test-id="{test_id}".*?<dd[^>]*>\s*(.*?)\s*</dd>', text, re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else None


def parse_company(text: str) -> dict:
    size = _dd(text, "about-us__size") or ""
    nums = [int(re.sub(r"[.,]", "", n)) for n in re.findall(r"\d[\d.,]*", size)]   # "1,000" (en) or "1.000" (es)
    country = re.search(r'"addressCountry":"([A-Z]{2})"', text)
    return {"size": size or None, "max_employees": max(nums) if nums else None,
            "industry": _dd(text, "about-us__industry"), "hq": _dd(text, "about-us__headquarters"),
            "country": country.group(1) if country else None}


def company(url: str) -> dict | None:
    r = _get(url.split("?")[0])
    if r is None or r.status_code != 200:
        return None
    info = parse_company(r.text)
    return info if info["size"] or info["industry"] else None


def local_consultancy(info: dict | None, cfg: dict) -> bool:
    """IT-services/staffing firm headquartered in LATAM and below the size threshold (low-pay pattern)."""
    if not info or not info.get("industry") or not re.search(cfg["industries"], info["industry"], re.I):
        return False
    local = (info.get("country") or "") in cfg["countries"]
    small = info.get("max_employees") is not None and info["max_employees"] <= cfg["max_employees"]
    tiny = info.get("max_employees") is not None and info["max_employees"] <= cfg.get("tiny_employees", 0)
    return tiny or (local and small)
