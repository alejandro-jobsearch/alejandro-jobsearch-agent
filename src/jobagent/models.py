"""Normalized job posting shared by every source."""
import re
import unicodedata
from datetime import date

from pydantic import BaseModel


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


class Job(BaseModel):
    source: str                      # linkedin | indeed | google | greenhouse | lever | ashby
    company: str
    title: str
    location: str = ""
    url: str
    description: str = ""
    date_posted: date | None = None
    remote: bool | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    currency: str | None = None
    search_term: str | None = None

    @property
    def key(self) -> str:
        """Company + title, normalized: the same role seen on two boards collapses to one key."""
        return f"{_norm(self.company)}|{_norm(self.title)}"


class ScoredJob(BaseModel):
    job: Job
    applied_match: str | None = None
    company_history: list[str] = []     # other roles already applied at the same company
    score: int | None = None            # weighted rubric, operator cap applied
    band: str | None = None
    ats_before: int | None = None       # keyword coverage of the base CV
    ats_missing: list[str] = []
    llm: dict | None = None             # raw LLMScore
    error: str | None = None
