"""Public ATS job-board APIs (no auth): Greenhouse, Lever, Ashby."""
import html
import re
from datetime import date, datetime, timezone

import httpx

from jobagent.models import Job

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


PROVIDERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}
