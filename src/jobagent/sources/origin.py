"""The company's own posting behind a remote-board listing, for the location the company states.

We Work Remotely labels many US- or Europe-only roles "Anywhere in the World" (2026-11-22: 3 of 3 checked were
region-locked at the company's Greenhouse). Its job pages sit behind a Cloudflare challenge, so the posting is
found from an ATS link in the RSS text, or else by looking for the company's public Greenhouse, Lever or Ashby
board and matching the title. A bare "Remote" at the ATS does not confirm worldwide either.
"""
import html
import logging
import re

from jobagent.models import Job, _norm
from jobagent.sources import boards

log = logging.getLogger(__name__)

ANYWHERE = re.compile(r"anywhere|worldwide", re.I)
_GENERIC = re.compile(r"[\W_]*(?:(?:fully|100%)?\s*remote|remoto)?[\W_]*", re.I)
_SUFFIX = {"inc", "llc", "ltd", "gmbh", "corp", "co", "sa", "bv", "ag", "plc"}
_GH_JID = re.compile(r"gh_jid=(\d+)")
_ATS = ("greenhouse", "lever", "ashby")


def slugs(company: str) -> list[str]:
    """Board names a company is likely to use: "Grafana Labs" -> grafanalabs, grafana-labs, grafana."""
    words = [w for w in _norm(company).split() if w not in _SUFFIX]
    out = ["".join(words), "-".join(words), words[0] if words else ""]
    return [s for i, s in enumerate(out) if s and s not in out[:i]]


def confirms_region(location: str) -> bool:
    """The ATS location says who can apply (a place, or worldwide), not just "Remote"."""
    return bool(location.strip()) and not _GENERIC.fullmatch(location)


def _job_id(url: str) -> str | None:
    m = _GH_JID.search(url or "") or re.search(r"/jobs/(\d+)", url or "") or \
        re.search(r"/([0-9a-f]{8}-[0-9a-f-]{27,})", url or "")
    return m[1] if m else None


def _match(title: str, posted: list[Job], job_id: str | None) -> Job | None:
    if job_id:
        hit = [p for p in posted if _job_id(p.url) == job_id]
        if hit:
            return hit[0]
    t = _norm(title)
    exact = [p for p in posted if _norm(p.title) == t]
    if exact:
        return exact[0]
    near = [p for p in posted if t and (t in _norm(p.title) or _norm(p.title) in t)]
    return near[0] if len(near) == 1 else None    # "Account Executive" fits many regional variants: no guess


class Resolver:
    """Looks up each company's boards once per run."""

    def __init__(self):
        self.cache: dict[tuple[str, str], list[Job]] = {}

    def _board(self, provider: str, slug: str, company: str) -> list[Job]:
        k = (provider, slug)
        if k not in self.cache:
            try:
                self.cache[k] = boards.PROVIDERS[provider](slug, company)
            except Exception as e:      # mostly 404: no board under that name. Never lose the posting over it
                log.debug("[origin] %s/%s: %s", provider, slug, e)
                self.cache[k] = []
        return self.cache[k]

    def find(self, j: Job, raw: str = "") -> Job | None:
        """`raw`: the listing's HTML, whose links may point at the ATS (plain-text descriptions lose them)."""
        links = re.findall(r"https?://[^\s\"'<>)]+", html.unescape(f"{raw} {j.description}"))
        ats_links = [l for l in links if "gh_jid=" in l or boards.board_from_url(l)]
        job_id = next(filter(None, map(_job_id, ats_links)), None)
        tries = [(b["provider"], b["slug"]) for b in map(boards.board_from_url, ats_links)
                 if b and b["provider"] in _ATS]
        tries += [(p, s) for s in slugs(j.company) for p in _ATS]
        for provider, slug in dict.fromkeys(tries):
            if got := _match(j.title, self._board(provider, slug, j.company), job_id):
                return got
        return None

    def check(self, j: Job, raw: str = "") -> Job:
        """`j` with the company's location when found; flagged `region_unverified` unless that location names
        a place or says worldwide."""
        got = self.find(j, raw)
        if not got:
            return j.model_copy(update={"region_unverified": True})
        return j.model_copy(update={"location": got.location or j.location, "apply_url": got.url,
                                    "description": got.description or j.description,
                                    "region_unverified": not confirms_region(got.location)})
