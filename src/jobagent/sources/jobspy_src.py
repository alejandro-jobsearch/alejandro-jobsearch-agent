"""Job boards via JobSpy (LinkedIn/Indeed/Google guest endpoints, no login)."""
import datetime as dt
import logging
import math
import re
import time
from collections.abc import Callable

from jobagent.models import Job

log = logging.getLogger(__name__)
_JOB_ID = re.compile(r"linkedin\.com/jobs/view/(?:[^/?]*-)?(\d+)")


def _val(v):
    """pandas NaN/NaT -> None."""
    if v is None:
        return None
    try:
        return None if v != v or (isinstance(v, float) and math.isnan(v)) else v
    except (TypeError, ValueError):
        return v


def _queries(terms: list[str], locations: list[dict], rotate: bool) -> list[tuple[str, dict]]:
    """(term, location) pairs. With `rotate`, odd days start halfway through the list, so the pairs a time
    budget cuts off one day are searched first the next day."""
    pairs = [(t, loc) for t in terms for loc in locations
             if "terms" not in loc or t in loc["terms"]]  # a location may search a subset of `terms`
    if rotate and pairs and dt.date.today().toordinal() % 2:
        half = len(pairs) // 2
        pairs = pairs[half:] + pairs[:half]
    return pairs


def _linkedin_details() -> Callable[[str], dict]:
    """JobSpy's own LinkedIn job-page reader (description, pay, contract type). It is a private method: if a
    JobSpy upgrade renames it, postings keep coming without description instead of failing the run."""
    try:
        from jobspy.linkedin import LinkedIn
        from jobspy.model import ScraperInput

        scraper = LinkedIn()
        scraper.scraper_input = ScraperInput(fetch_description=True)
        return scraper._fetch_details
    except Exception as e:
        log.warning("[jobspy] LinkedIn job-page reader unavailable: %s", e)
        return lambda _id: {}


def _with_details(j: Job, d: dict) -> Job:
    pay = d.get("compensation")
    types = ",".join(t.value[0] for t in d.get("job_type") or [] if getattr(t, "value", None))
    return j.model_copy(update={
        "description": d.get("description") or j.description,
        "job_type": types or j.job_type,
        "salary_min": getattr(pay, "min_amount", None) or j.salary_min,
        "salary_max": getattr(pay, "max_amount", None) or j.salary_max,
        "currency": getattr(pay, "currency", None) or j.currency})


def search(terms: list[str], locations: list[dict], sites: list[str], hours_old: int,
           per_term: int, sleep: float = 4.0, fetch_description: bool = True,
           max_minutes: float | None = None, keep: Callable[[Job], bool] | None = None,
           details: Callable[[str], dict] | None = None, detail_sleep: float = 1.0) -> list[Job]:
    """`max_minutes`: stop starting new queries after this long, so the scoring that follows fits in the job's
    timeout.

    `keep` (2026-11-14): LinkedIn search pages are read without job pages, and the job page (description) is
    fetched only for postings that `keep` accepts (title prefilter, not seen before), once per run. Reading every
    job page made each query take ~30 s and the 50-min budget left ~36 of 141 queries out each day, while 72% of
    the postings were then dropped by title. Without `keep`, `fetch_description` decides as before."""
    from jobspy import scrape_jobs  # heavy import (pandas), only when used

    jobs: list[Job] = []
    pairs = _queries(terms, locations, rotate=max_minutes is not None)
    lazy = keep is not None and fetch_description
    read, fetched = (details or _linkedin_details()) if lazy else None, {}
    start = time.monotonic()
    for i, (term, loc) in enumerate(pairs):
        if max_minutes is not None and time.monotonic() - start > max_minutes * 60:
            log.warning("[jobspy] time budget of %s min reached: %d of %d queries left for the next run",
                        max_minutes, len(pairs) - i, len(pairs))
            break
        try:
            df = scrape_jobs(site_name=sites, search_term=term, location=loc["name"],
                             is_remote=loc.get("remote", False), results_wanted=per_term,
                             hours_old=hours_old, linkedin_fetch_description=fetch_description and not lazy,
                             country_indeed=loc.get("country_indeed", "peru"), verbose=0)
        except Exception as e:  # 429 and friends: keep going with the rest
            log.warning("[%s / %s] %s", term, loc["name"], e)
            continue
        log.info("[%s / %s] %d", term, loc["name"], len(df))
        for r in df.to_dict("records"):
            r = {k: _val(v) for k, v in r.items()}
            j = Job(
                source=str(r.get("site") or "jobspy"), company=str(r.get("company") or ""),
                title=str(r.get("title") or ""), location=str(r.get("location") or ""),
                url=str(r.get("job_url")), description=str(r.get("description") or ""),
                date_posted=r.get("date_posted"), remote=r.get("is_remote"),
                salary_min=r.get("min_amount"), salary_max=r.get("max_amount"),
                currency=r.get("currency"), search_term=term, job_type=r.get("job_type"),
                company_url=r.get("company_url"), apply_url=r.get("job_url_direct"))
            if lazy and j.source == "linkedin" and not j.description and (m := _JOB_ID.search(j.url)) and keep(j):
                if m[1] not in fetched:
                    fetched[m[1]] = read(m[1])
                    time.sleep(detail_sleep)
                j = _with_details(j, fetched[m[1]])
            jobs.append(j)
        time.sleep(sleep)
    if lazy:
        log.info("[jobspy] LinkedIn job pages read: %d of %d postings", len(fetched), len(jobs))
    return jobs
