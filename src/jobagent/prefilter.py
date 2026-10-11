"""Deterministic, zero-LLM filters: title include/exclude, contract type, company blocklist and geography."""
import re

from jobagent.models import Job


def _rx(patterns: list[str] | None) -> re.Pattern | None:
    return re.compile("|".join(patterns), re.I) if patterns else None


class Prefilter:
    def __init__(self, cfg: dict):
        self.include = _rx(cfg["title_include"])
        self.exclude = _rx(cfg["title_exclude"])
        self.domain = _rx(cfg.get("title_domain"))
        self.geo_keep = _rx(cfg["geo_keep"])
        self.geo_drop = _rx(cfg.get("geo_drop"))
        self.company_exclude = _rx(cfg.get("company_exclude"))
        self.job_types = {t.lower() for t in cfg.get("job_type_exclude", [])}

    def title_ok(self, j: Job) -> bool:
        t = j.title
        return (bool(self.include.search(t)) and not self.exclude.search(t)
                and (self.domain is None or bool(self.domain.search(t))))

    def type_ok(self, j: Job) -> bool:
        """JobSpy job_type may list several ("fulltime, contract"); drop only if every listed type is excluded."""
        types = {t.strip().lower() for t in (j.job_type or "").split(",") if t.strip()}
        return not types or not types <= self.job_types

    def company_ok(self, j: Job) -> bool:
        return not (self.company_exclude and self.company_exclude.search(j.company))

    def geo_ok(self, j: Job) -> bool:
        loc = j.location or ""
        if self.geo_keep.search(loc):
            return True
        if self.geo_drop and self.geo_drop.search(loc):
            return False
        return loc.strip() == ""

    def __call__(self, j: Job) -> bool:
        return self.title_ok(j) and self.type_ok(j) and self.company_ok(j) and self.geo_ok(j)
