"""Job boards via JobSpy (LinkedIn/Indeed/Google guest endpoints, no login)."""
import datetime as dt
import logging
import math
import time

from jobagent.models import Job

log = logging.getLogger(__name__)


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


def search(terms: list[str], locations: list[dict], sites: list[str], hours_old: int,
           per_term: int, sleep: float = 4.0, fetch_description: bool = True,
           max_minutes: float | None = None) -> list[Job]:
    """`max_minutes`: stop starting new queries after this long (each takes ~30 s with descriptions), so the
    scoring that follows fits in the job's timeout."""
    from jobspy import scrape_jobs  # heavy import (pandas), only when used

    jobs: list[Job] = []
    pairs = _queries(terms, locations, rotate=max_minutes is not None)
    start = time.monotonic()
    for i, (term, loc) in enumerate(pairs):
        if max_minutes is not None and time.monotonic() - start > max_minutes * 60:
            log.warning("[jobspy] time budget of %s min reached: %d of %d queries left for the next run",
                        max_minutes, len(pairs) - i, len(pairs))
            break
        try:
            df = scrape_jobs(site_name=sites, search_term=term, location=loc["name"],
                             is_remote=loc.get("remote", False), results_wanted=per_term,
                             hours_old=hours_old, linkedin_fetch_description=fetch_description,
                             country_indeed=loc.get("country_indeed", "peru"), verbose=0)
        except Exception as e:  # 429 and friends: keep going with the rest
            log.warning("[%s / %s] %s", term, loc["name"], e)
            continue
        log.info("[%s / %s] %d", term, loc["name"], len(df))
        for r in df.to_dict("records"):
            r = {k: _val(v) for k, v in r.items()}
            jobs.append(Job(
                source=str(r.get("site") or "jobspy"), company=str(r.get("company") or ""),
                title=str(r.get("title") or ""), location=str(r.get("location") or ""),
                url=str(r.get("job_url")), description=str(r.get("description") or ""),
                date_posted=r.get("date_posted"), remote=r.get("is_remote"),
                salary_min=r.get("min_amount"), salary_max=r.get("max_amount"),
                currency=r.get("currency"), search_term=term, job_type=r.get("job_type"),
                company_url=r.get("company_url"), apply_url=r.get("job_url_direct")))
        time.sleep(sleep)
    return jobs
