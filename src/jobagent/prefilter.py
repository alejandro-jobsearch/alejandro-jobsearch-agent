"""Deterministic, zero-LLM filters: title include/exclude and geography."""
import re

from jobagent.models import Job


class Prefilter:
    def __init__(self, cfg: dict):
        self.include = re.compile("|".join(cfg["title_include"]), re.I)
        self.exclude = re.compile("|".join(cfg["title_exclude"]), re.I)
        self.domain = re.compile("|".join(cfg["title_domain"]), re.I) if cfg.get("title_domain") else None
        self.geo_keep = re.compile("|".join(cfg["geo_keep"]), re.I)
        self.geo_drop = re.compile("|".join(cfg["geo_drop"]), re.I) if cfg.get("geo_drop") else None

    def title_ok(self, j: Job) -> bool:
        t = j.title
        return (bool(self.include.search(t)) and not self.exclude.search(t)
                and (self.domain is None or bool(self.domain.search(t))))

    def geo_ok(self, j: Job) -> bool:
        loc = j.location or ""
        if self.geo_keep.search(loc):
            return True
        if self.geo_drop and self.geo_drop.search(loc):
            return False
        return loc.strip() == ""

    def __call__(self, j: Job) -> bool:
        return self.title_ok(j) and self.geo_ok(j)
