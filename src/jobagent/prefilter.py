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
        self.geo_block = _rx(cfg.get("geo_block"))
        self.geo_drop = _rx(cfg.get("geo_drop"))
        self.rescue_places = _rx(cfg.get("geo_rescue_places"))
        self.rescue_text = _rx(cfg.get("geo_rescue_text"))
        self.geo_local = _rx(cfg.get("geo_local"))
        self.eligibility_drop = _rx(cfg.get("eligibility_drop"))
        self.company_exclude = _rx(cfg.get("company_exclude"))
        self.job_types = {t.lower() for t in cfg.get("job_type_exclude", [])}
        reloc = cfg.get("relocation") or {}
        self.reloc_title = _rx(reloc.get("title"))
        self.reloc_places = _rx(reloc.get("places"))
        self.reloc_exclude = _rx(reloc.get("title_exclude"))
        self.sponsorship_drop = _rx(reloc.get("sponsorship_drop"))

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

    def rescued(self, j: Job) -> bool:
        """Regional roles filed under one country whose text opens them to the region ("flexible location")."""
        return bool(self.rescue_places and self.rescue_text and self.rescue_places.search(j.location or "")
                    and self.rescue_text.search(j.description or ""))

    def relocation(self, j: Job) -> bool:
        """A leadership role (jefatura/gerencia and up) on site in a country the candidate would move to
        (2026-11-14). Not for places geo_keep already keeps: those are local or open remote roles."""
        loc = j.location or ""
        return bool(self.reloc_title and self.reloc_places and self.reloc_title.search(j.title)
                    and not (self.reloc_exclude and self.reloc_exclude.search(j.title))
                    and self.reloc_places.search(loc) and not self.geo_keep.search(loc))

    def geo_ok(self, j: Job) -> bool:
        loc = j.location or ""
        if self.geo_block and self.geo_block.search(loc):   # mis-geocoded places win over geo_keep
            return False
        if self.geo_keep.search(loc):
            return True
        if self.rescued(j) or self.relocation(j):
            return True
        if self.geo_drop and self.geo_drop.search(loc):
            return False
        return loc.strip() == ""

    def eligible_ok(self, j: Job) -> bool:
        """Remote postings from outside the local market that state they only hire elsewhere. Saves the LLM call;
        the LLM's `remote_eligibility` catches what these patterns miss. A relocation role is on site abroad by
        design ("must be based in New York" is expected): it is dropped only when it rules out visa sponsorship."""
        text = f"{j.title}\n{j.location}\n{j.description}"
        if self.relocation(j):
            return not (self.sponsorship_drop and self.sponsorship_drop.search(text))
        if not self.eligibility_drop or (self.geo_local and self.geo_local.search(j.location or "")):
            return True
        return not self.eligibility_drop.search(text)

    def __call__(self, j: Job) -> bool:
        return (self.title_ok(j) and self.type_ok(j) and self.company_ok(j) and self.geo_ok(j)
                and self.eligible_ok(j))
