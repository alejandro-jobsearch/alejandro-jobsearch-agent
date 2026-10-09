"""Job boards via JobSpy (LinkedIn/Indeed/Google guest endpoints, no login)."""
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


def search(terms: list[str], locations: list[dict], sites: list[str], hours_old: int,
           per_term: int, sleep: float = 4.0, fetch_description: bool = True) -> list[Job]:
    from jobspy import scrape_jobs  # heavy import (pandas), only when used

    jobs: list[Job] = []
    for term in terms:
        for loc in locations:
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
                    currency=r.get("currency"), search_term=term))
            time.sleep(sleep)
    return jobs
