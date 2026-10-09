"""LLM match scoring against the candidate's rubric (one call per posting, JSON out)."""
import json
import logging
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from jobagent.llm import client, model
from jobagent.models import Job

log = logging.getLogger(__name__)

DIMENSIONS = ("seniority", "domain", "industry", "operator_vendor", "geo", "compensation")


class Keyword(BaseModel):
    term: str
    importance: Literal["must", "nice"]
    aliases: list[str] = []


class LLMScore(BaseModel):
    dims: dict[str, int] = Field(description="0-100 per dimension")
    requires_operator_leadership: bool
    lane: Literal["A", "B", "C", "none"]
    lang: Literal["en", "es"]
    english_work: Literal["yes", "no", "unspecified"]
    seniority_real: str
    gaps: list[str]
    summary: str
    keywords: list[Keyword]


SYSTEM = """You are a strict recruiter-side evaluator. Score ONE job posting for ONE candidate using the
candidate's criteria document below. Be conservative: over-estimating fit is the main failure mode.

=== CANDIDATE CRITERIA ===
{criteria}
=== END CRITERIA ===

Return ONLY a JSON object with exactly these keys:
- "dims": object with integer 0-100 scores for {dims}
  (seniority = level fit; domain = AI/cloud/architecture/governance fit; industry; operator_vendor = apply the
  anti-false-positive rule; geo = location/modality fit; compensation = 100 if >= floor or not published, 0 if below)
- "requires_operator_leadership": true if the posting requires having LED technology INSIDE a financial institution
- "lane": "A" | "B" | "C" | "none" (lanes from the criteria)
- "lang": language the posting is written in, "en" or "es"
- "english_work": "yes" | "no" | "unspecified" (English as day-to-day working language)
- "seniority_real": one short phrase with the REAL level after reading the duties (not the title)
- "gaps": list of concrete requirements the candidate does not evidence (short phrases, max 6)
- "summary": 2 sentences, in Spanish, on why it fits or not
- "keywords": 8-20 ATS keywords from the posting: {{"term", "importance": "must"|"nice", "aliases": [synonyms or
  translations a recruiter would accept, incl. English/Spanish variants]}}. Skills, tools, certifications,
  methodologies, domain terms. No soft skills, no company name."""


def _user(j: Job) -> str:
    return json.dumps({"company": j.company, "title": j.title, "location": j.location, "source": j.source,
                       "salary": [j.salary_min, j.salary_max, j.currency] if j.salary_min else None,
                       "description": j.description[:12000]}, ensure_ascii=False)


def score(j: Job, criteria: str) -> LLMScore:
    msgs = [{"role": "system", "content": SYSTEM.format(criteria=criteria, dims=", ".join(DIMENSIONS))},
            {"role": "user", "content": _user(j)}]
    last = None
    for attempt in range(2):
        r = client().chat.completions.create(model=model(), messages=msgs, max_tokens=6000,
                                             response_format={"type": "json_object"}, temperature=0.2)
        try:
            return LLMScore.model_validate_json(r.choices[0].message.content)
        except ValidationError as e:
            last = e
            log.warning("invalid JSON for %s (attempt %d): %s", j.url, attempt + 1, e.errors()[:2])
    raise last


def weighted(dims: dict[str, int], weights: dict[str, float], operator_cap: bool, cap: int) -> int:
    total = sum(weights[d] * dims.get(d, 0) for d in weights) / sum(weights.values())
    return round(min(total, cap) if operator_cap else total)


def band(score: int) -> str:
    return "90+" if score >= 90 else "75-89" if score >= 75 else "60-74" if score >= 60 else "<60"
