"""Bumeran Perú via its public sitemaps (listed in robots.txt).

The search API and the posting pages sit behind a Cloudflare bot challenge, so this source only reads the
sitemaps, which are plain files regenerated daily (~05:00 Lima). Each posting URL is
`/empleos/<title>-<company>-<id>.html`; the company part is recognised against the company sitemap
(`/perfiles/empresa_<company>_<id>.html`). There is no description, location or date: the title is all we get,
so the LLM scores on the title alone and the Issue links to the posting.
"""
import logging
import re

import httpx

from jobagent.models import Job, _norm

log = logging.getLogger(__name__)

BASE = "https://www.bumeran.com.pe"
JOBS_SITEMAP = f"{BASE}/sitemap_avisos_bum.xml"
COMPANIES_SITEMAP = f"{BASE}/sitemap_empresas_bum.xml"
TIMEOUT = httpx.Timeout(60)
HEADERS = {"User-Agent": "jobagent (+https://github.com/alejandro-jobsearch/alejandro-jobsearch-agent)"}
NO_DESCRIPTION = "(Bumeran no permite leer el aviso de forma automatizada: solo se conoce el título. Ver el enlace.)"

_JOB = re.compile(r"<loc>https://www\.bumeran\.com\.pe/empleos/(.+?)-(\d+)\.html</loc>")
_COMPANY = re.compile(r"<loc>https://www\.bumeran\.com\.pe/perfiles/empresa_(.+?)_\d+\.html</loc>")


def _words(slug: str) -> str:
    return re.sub(r"\s+", " ", slug.replace("-", " ")).strip()


def companies(xml: str) -> list[str]:
    """Company slugs, longest first so "manpowergroup-peru" wins over "manpowergroup"."""
    return sorted(set(_COMPANY.findall(xml)), key=len, reverse=True)


def parse(xml: str, company_slugs: list[str]) -> list[Job]:
    """Jobs from the postings sitemap, newest (highest id) first."""
    out = []
    for slug, aviso_id in _JOB.findall(xml):
        company = next((c for c in company_slugs if slug.endswith("-" + c)), None)
        title = slug[: -len(company) - 1] if company else slug
        out.append((int(aviso_id), Job(
            source="bumeran", company=_words(company).title() if company else "", title=_words(title).capitalize(),
            location="Perú", url=f"{BASE}/empleos/{slug}-{aviso_id}.html", description=NO_DESCRIPTION)))
    return [j for _, j in sorted(out, key=lambda t: t[0], reverse=True)]


STOPWORDS = {"de", "del", "la", "el", "los", "las", "y", "e", "en", "of", "and", "the", "a"}


def _stem(w: str) -> str:
    """arquitecto/arquitecta/arquitectoa, gerente/gerentea: compare without the trailing gender letter."""
    return w[:-1] if len(w) > 4 and w[-1] in "oa" else w


def _hit(w: str, words: list[str]) -> bool:
    # acronyms (TI, IA, CTO) must match a whole word: "ti" is not "tienda"
    return w in words if len(w) <= 3 else any(t.startswith(w) for t in words)


def matches(title: str, terms: list[str]) -> str | None:
    """First search term whose every (non-stop) word starts a word of the title, the way a keyword search
    would match it; the sitemap has no search, so this stands in for it."""
    words = [_stem(w) for w in _norm(title).split()]
    for term in terms:
        need = [_stem(w) for w in _norm(term).split() if w not in STOPWORDS]
        if need and all(_hit(w, words) for w in need):
            return term
    return None


def _get(url: str) -> str:
    r = httpx.get(url, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return r.text


def search(terms: list[str], keep=None, max_jobs: int = 60) -> list[Job]:
    """The sitemap is the whole catalogue (tens of thousands of postings), not a daily window: keep the postings
    whose title matches a search term and that `keep(job)` accepts (the title prefilter), and of those the
    `max_jobs` newest by id. `seen` state drops the ones already processed on previous days."""
    jobs = parse(_get(JOBS_SITEMAP), companies(_get(COMPANIES_SITEMAP)))
    kept = []
    for j in jobs:
        if (term := matches(j.title, terms)) and (keep is None or keep(j)):
            kept.append(j.model_copy(update={"search_term": term}))
    log.info("[bumeran] %d postings, %d match, keeping %d", len(jobs), len(kept), min(len(kept), max_jobs))
    return kept[:max_jobs]
