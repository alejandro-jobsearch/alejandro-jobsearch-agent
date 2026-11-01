"""Bumeran Perú via its public sitemaps (listed in robots.txt).

The search API and the posting pages sit behind a Cloudflare bot challenge, so this source only reads the
sitemaps, which are plain files regenerated daily (~05:00 Lima). Each posting URL is
`/empleos/<title>-<company>-<id>.html`; the company part is recognised against the company sitemap
(`/perfiles/empresa_<company>_<id>.html`), which lists only ~800 companies, plus `RECRUITERS` and the
`bumeran.companies` config list. There is no description, location or date, so these postings are not scored:
they go to the digest's triage list, and the full posting comes back through the manual intake form
(`jobagent.intake`). Ids grow with each new posting, so a stored watermark keeps only those published since
the last run.
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
# frequent advertisers missing from the company sitemap (recruiters posting for confidential clients)
RECRUITERS = ["empresa-no-validada", "experis-peru", "adecco-bcp", "adecco-peru", "bluetab-solutions",
              "era-talent-sac", "bcp"]

_COMPANY = re.compile(r"<loc>https://www\.bumeran\.com\.pe/perfiles/empresa_(.+?)_\d+\.html</loc>")


def _spaced(slug: str) -> str:
    return re.sub(r"\s+", " ", slug.replace("-", " ")).strip()


def companies(xml: str) -> list[str]:
    """Company slugs, longest first so "manpowergroup-peru" wins over "manpowergroup"."""
    return sorted(set(_COMPANY.findall(xml)), key=len, reverse=True)


def aviso_id(url: str) -> int:
    return int(url.rsplit("-", 1)[-1].removesuffix(".html"))


def parse(xml: str, company_slugs: list[str]) -> list[Job]:
    """Jobs from the postings sitemap, newest (highest id) first."""
    company_slugs = sorted(set(company_slugs) | set(RECRUITERS), key=len, reverse=True)
    out = []
    for slug, aviso_id in _JOB.findall(xml):
        company = next((c for c in company_slugs if slug.endswith("-" + c)), None)
        title = slug[: -len(company) - 1] if company else slug
        out.append((int(aviso_id), Job(
            source="bumeran", company=_spaced(company).title() if company else "", title=_spaced(title).capitalize(),
            location="Perú", url=f"{BASE}/empleos/{slug}-{aviso_id}.html", description=NO_DESCRIPTION)))
    return [j for _, j in sorted(out, key=lambda t: t[0], reverse=True)]


STOPWORDS = {"de", "del", "la", "el", "los", "las", "y", "e", "en", "of", "and", "the", "a", "para", "con"}
# seniority words that sit inside a title phrase without changing it: "Arquitecto senior de soluciones"
FILLER = {"senior", "sr", "ssr", "semisenior", "semi", "junior", "jr", "lead", "principal", "o", "oa"}


def _stem(w: str) -> str:
    """arquitecto/arquitecta/arquitectoa, gerente/gerentea: compare without the trailing gender letter."""
    return w[:-1] if len(w) > 4 and w[-1] in "oa" else w


def _forms(w: str) -> set[str]:
    """A word, its gender-less stem and its singular: "soluciones" ~ "solucion", "tecnologias" ~ "tecnologia".
    No prefix matching: "arquitecturas" is not "arquitecto"."""
    out = {w, _stem(w)}
    for suffix in ("es", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            out |= {w[: -len(suffix)], _stem(w[: -len(suffix)])}
    return out


def _words(text: str, skip: set[str]) -> list[set[str]]:
    return [_forms(w) for w in _norm(text).split() if w not in skip]


def matches(title: str, terms: list[str]) -> str | None:
    """First search term found as a phrase in the title, the way a keyword search would match it: its words in
    order and next to each other, ignoring articles/prepositions and seniority words, with gender and plural
    folded. The sitemap has no search, so this stands in for it."""
    words = _words(title, STOPWORDS | FILLER)
    for term in terms:
        need = _words(term, STOPWORDS)
        if need and any(all(need[k] & words[i + k] for k in range(len(need)))
                        for i in range(len(words) - len(need) + 1)):
            return term
    return None


def _get(url: str) -> str:
    r = httpx.get(url, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return r.text


def search(terms: list[str], keep=None, max_jobs: int = 25, after_id: int = 0,
           extra_companies: list[str] = ()) -> tuple[list[Job], int]:
    """The sitemap is the whole catalogue (tens of thousands of postings), not a daily window: keep the postings
    newer than `after_id` whose title matches a search term and that `keep(job)` accepts (the title prefilter),
    and of those the `max_jobs` newest. Returns them and the highest id in the sitemap, the next watermark."""
    slugs = companies(_get(COMPANIES_SITEMAP)) + [_norm(c).replace(" ", "-") for c in extra_companies]
    jobs = parse(_get(JOBS_SITEMAP), slugs)
    newest = aviso_id(jobs[0].url) if jobs else after_id
    fresh = [j for j in jobs if aviso_id(j.url) > after_id]
    kept = []
    for j in fresh:
        if (term := matches(j.title, terms)) and (keep is None or keep(j)):
            kept.append(j.model_copy(update={"search_term": term}))
    log.info("[bumeran] %d postings, %d new since id %d, %d match, keeping %d",
             len(jobs), len(fresh), after_id, len(kept), min(len(kept), max_jobs))
    return kept[:max_jobs], max(newest, after_id)
