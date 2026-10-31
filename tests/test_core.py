from jobagent import ats, scoring
from jobagent.models import Job
from jobagent.prefilter import Prefilter

CV = "Led enterprise architecture (TOGAF 9) and data governance programs; defined technology roadmaps."


def test_ats_exact_alias_and_token_match():
    kws = [
        {"term": "TOGAF", "importance": "must"},
        {"term": "gobierno de datos", "importance": "must", "aliases": ["data governance"]},
        {"term": "roadmap tecnológico", "importance": "nice", "aliases": ["technology roadmap"]},
        {"term": "Kafka", "importance": "nice"},
    ]
    pct, missing = ats.coverage(kws, CV)
    assert missing == ["Kafka"]
    assert pct == round(100 * 5 / 6)


def test_ats_single_word_needs_exact_match():
    assert ats.coverage([{"term": "AWS", "importance": "must"}], "Worked with AWSome teams")[0] == 0


def test_weighted_score_and_operator_cap():
    w = {"seniority": 25, "domain": 25, "industry": 20, "operator_vendor": 15, "geo": 10, "compensation": 5}
    dims = dict.fromkeys(w, 90)
    assert scoring.weighted(dims, w, operator_cap=False, cap=70) == 90
    assert scoring.weighted(dims, w, operator_cap=True, cap=70) == 70
    assert scoring.band(75) == "75-89" and scoring.band(59) == "<60"


def test_fenced_json_is_unwrapped():
    assert scoring._json_body('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_prefilter_title_domain_geo():
    pf = Prefilter({
        "title_include": [r"\b(head|gerente|architect)"],
        "title_exclude": [r"\bjunior\b"],
        "title_domain": [r"\b(ai|ti|architect)"],
        "geo_keep": ["peru|lima"],
        "geo_drop": ["."],
    })
    mk = lambda t, loc="Lima, Peru": Job(source="x", company="c", title=t, location=loc, url=t)
    assert pf(mk("Head of AI"))
    assert not pf(mk("Gerente de Recursos Humanos"))      # no domain word
    assert not pf(mk("Junior Architect"))                 # excluded
    assert not pf(mk("Head of AI", "Mexico City"))        # geography
    assert pf(mk("Head of AI", ""))                       # unknown location kept


def test_tracker_select_threshold_and_watch_word_boundary():
    from jobagent.models import ScoredJob
    from jobagent.tracker import select

    mk = lambda company, score: ScoredJob(job=Job(source="x", company=company, title="t", url=company), score=score)
    cfg = {"min_score": 75, "watch_min": 60, "watch_companies": ["EY", "Globant"]}
    picked = select([mk("Acme", 80), mk("Globant", 70), mk("Keystone", 70), mk("EY Peru", 65), mk("Foo", 50)], cfg)
    assert [(s.job.company, w) for s, w in picked] == [("Acme", False), ("Globant", True), ("EY Peru", True)]


def test_prefilter_job_type_and_company_blocklist():
    pf = Prefilter({"title_include": ["head"], "title_exclude": [r"\btemporal\b"], "geo_keep": ["lima"],
                    "job_type_exclude": ["temporary", "internship"], "company_exclude": [r"\bnovacomp\b"]})
    mk = lambda **kw: Job(source="x", company=kw.pop("company", "Acme"), title=kw.pop("title", "Head of AI"),
                          location="Lima", url="u", **kw)
    assert pf(mk(job_type="fulltime"))
    assert pf(mk(job_type="fulltime, temporary"))          # mixed: keep
    assert not pf(mk(job_type="temporary"))
    assert not pf(mk(title="Head of AI (temporal)"))
    assert not pf(mk(company="Novacomp"))


def test_linkedin_company_parse_and_local_consultancy():
    from jobagent import linkedin

    page = ('<div data-test-id="about-us__size"><dt>Company size</dt><dd class="x">\n 501-1,000 employees\n</dd></div>'
            '<div data-test-id="about-us__industry"><dt>Industry</dt><dd>IT Services and IT Consulting</dd></div>'
            '{"addressCountry":"CR"}')
    info = linkedin.parse_company(page)
    assert info == {"size": "501-1,000 employees", "max_employees": 1000, "industry": "IT Services and IT Consulting",
                    "hq": None, "country": "CR"}
    cfg = {"industries": "IT Services|Consulting|Staffing", "countries": ["PE", "CO", "CR"], "max_employees": 5000,
           "tiny_employees": 200}
    assert linkedin.parse_company(page.replace("1,000", "1.000"))["max_employees"] == 1000   # es.linkedin
    assert linkedin.local_consultancy(info, cfg)                                     # regional body shop
    assert not linkedin.local_consultancy({**info, "country": "US"}, cfg)            # US consultancy, mid-size
    assert linkedin.local_consultancy({**info, "country": "US", "max_employees": 200}, cfg)   # tiny anywhere
    assert not linkedin.local_consultancy({**info, "max_employees": 10001}, cfg)     # Globant-size
    assert not linkedin.local_consultancy({**info, "industry": "Financial Services"}, cfg)
    assert not linkedin.local_consultancy(None, cfg)


def test_job_url_from_issue_body():
    from jobagent.tracker import job_url

    assert job_url('**[T](https://a/1)** x\n<!-- jobagent-meta: {"url": "https://li/2"} -->') == "https://li/2"
    assert job_url("**[T](https://a/1)** x") == "https://a/1"
    assert job_url("no link") is None


def test_select_skips_temporary():
    from jobagent.models import ScoredJob
    from jobagent.tracker import select

    s = ScoredJob(job=Job(source="x", company="Acme", title="t", url="u"), score=90, llm={"temporary": True})
    assert select([s], {"min_score": 75, "watch_min": 60}) == []


def test_select_skips_hard_exclusion_even_for_watched_company():
    from jobagent.models import ScoredJob
    from jobagent.tracker import select

    cfg = {"min_score": 75, "watch_min": 60, "watch_companies": ["Indra"]}
    excl = ScoredJob(job=Job(source="x", company="Indra", title="t", url="u"), score=64,
                     llm={"hard_exclusion": True, "exclusion_reason": "rol centrado en Azure"})
    high = ScoredJob(job=Job(source="x", company="Acme", title="t", url="u2"), score=90,
                     llm={"hard_exclusion": True})
    ok = ScoredJob(job=Job(source="x", company="Indra", title="t", url="u3"), score=64,
                   llm={"hard_exclusion": False})
    assert select([excl, high, ok], cfg) == [(ok, True)]


def test_llmscore_hard_exclusion_defaults():
    from jobagent.scoring import SYSTEM, LLMScore

    base = {"dims": {}, "requires_operator_leadership": False, "lane": "none", "lang": "es",
            "english_work": "unspecified", "seniority_real": "x", "gaps": [], "summary": "s", "keywords": []}
    m = LLMScore.model_validate(base)
    assert m.hard_exclusion is False and m.exclusion_reason == ""
    m = LLMScore.model_validate({**base, "hard_exclusion": True, "exclusion_reason": "cert vencida"})
    assert m.hard_exclusion and m.exclusion_reason == "cert vencida"
    assert '"hard_exclusion"' in SYSTEM and '"exclusion_reason"' in SYSTEM


def test_hirehive_pages_and_parses(monkeypatch):
    from jobagent.sources import boards

    pages = {
        "https://acme.hirehive.com/api/v1/jobs": {"jobs": [{
            "title": "Arquitecto de Tecnología", "location": "Lima", "country": {"name": "Peru"},
            "description": {"html": "<p>Azure</p><li>APIs</li>"}, "publishedDate": "1789765753",
            "hostedUrl": "https://acme.hirehive.com/a"}], "nextPage": "https://acme.hirehive.com/api/v1/jobs?skip=1"},
        "https://acme.hirehive.com/api/v1/jobs?skip=1": {"jobs": [{
            "title": "CTO", "location": None, "country": None, "description": None, "publishedDate": None,
            "hostedUrl": "https://acme.hirehive.com/b"}], "nextPage": None},
    }

    class R:
        def __init__(self, d): self.d = d
        def raise_for_status(self): pass
        def json(self): return self.d

    monkeypatch.setattr(boards.httpx, "get", lambda url, **kw: R(pages[url]))
    a, b = boards.hirehive("acme", "Acme")
    assert (a.location, a.description, str(a.date_posted)) == ("Lima, Peru", "Azure\n- APIs", "2026-09-18")
    assert (b.title, b.location, b.date_posted) == ("CTO", "", None)


def test_ats_mirror_finds_copied_runs():
    jd = "Liderar la gestión y priorización de la demanda tecnológica según valor, capacidad y recursos disponibles."
    cv = "Priorizo la demanda tecnológica según valor, capacidad y recursos disponibles para clientes telco."
    pct, runs = ats.mirror(cv, jd)
    assert runs == ["la demanda tecnologica segun valor capacidad y recursos disponibles"]
    assert pct == round(100 * 9 / 13)
    assert ats.mirror("Defined cloud reference architectures.", jd)[0] == 0


def test_cvcheck_blocks_mirrored_cv():
    from jobagent import cvcheck

    experience = {
        "forbidden_claims": [{"pattern": r"\b(1[0-9]|[6-9]) (anos|years)\b[^.\d]{0,80}financ", "why": "~5.5 años"}],
        "roles": [{"title": {"en": "Cloud Solutions Architect", "es": "Arquitecto de Soluciones Cloud"},
                   "forbid": [{"pattern": r"\b(banca|seguros)\b", "why": "solo telco"}]}],
    }
    creds = [{"name": "Scrum Master Certified (SMC)", "status": "expired", "expires": "2024-01-05",
              "cv_aliases": ["Scrum Master"]},
             {"name": "AWS Certified Cloud Practitioner", "status": "expired", "expires": "2024-10-25"}]
    plan = {"headline": "Digital Domain Lead | Estrategia Tecnológica",
            "summary": "11 años liderando iniciativas en entornos financieros.",
            "roles": {"Arquitecto de Soluciones Cloud": ["Portafolio con foco en banca y seguros."]}}
    text = "\n".join([plan["headline"], plan["summary"], *plan["roles"]["Arquitecto de Soluciones Cloud"],
                      "Certificaciones: Scrum Master, AWS Certified Cloud Practitioner (2021–2024)."])
    errors = cvcheck.check(plan, text, {"title": "Digital Domain Lead", "description": ""},
                           experience, creds)["errors"]
    assert len(errors) == 4
    assert [e.split(":")[0] for e in errors] == ["titular usa el cargo de la vacante («Digital Domain Lead») como si fuera propio",
                                                 "certificación vencida sin años", "CV", "Arquitecto de Soluciones Cloud"]


def test_cvcheck_headline_allows_held_title():
    from jobagent import cvcheck

    assert cvcheck.headline("Arquitecto de Soluciones Cloud | IA", "Arquitecto de Soluciones Cloud",
                            ["Arquitecto de Soluciones Cloud"]) == []
    assert cvcheck.headline("Enterprise AI & Cloud Architecture Leader", "Digital Domain Lead", []) == []


def test_company_pause_needs_two_failures_and_respects_referrals(tmp_path):
    from jobagent.state import Context

    ctx = Context(data_dir=tmp_path, criteria="", cv={}, applied=[
        {"company": "Acme Bank", "role": "A", "note": "SIN RESPUESTA — cerrada"},
        {"company": "Acme Bank", "role": "B", "note": "RECHAZADA 2026-10-26"},
        {"company": "Globex Capital", "role": "C", "note": "RECHAZADA"},
        {"company": "Globex Capital", "role": "D", "note": "POSTULADO"},
    ])
    mk = lambda c: Job(source="x", company=c, title="t", url=c)
    assert "solo con referido" in ctx.paused(mk("Acme Bank"), 2, [])
    assert ctx.paused(mk("Acme Bank"), 2, ["Acme Bank"]) is None
    assert ctx.paused(mk("Globex Capital"), 2, []) is None
    assert ctx.paused(mk("Globex"), 1, []) is None          # group name is not the subsidiary


def test_llmscore_internal_bank_role_defaults_false():
    s = scoring.LLMScore.model_validate({"dims": {}, "requires_operator_leadership": False, "lane": "none",
                                         "lang": "es", "english_work": "no", "seniority_real": "", "gaps": [],
                                         "summary": "", "keywords": []})
    assert s.internal_bank_role is False


BUMERAN_JOBS = """<?xml version="1.0" encoding="utf-8"?><urlset>
  <url><loc>https://www.bumeran.com.pe/empleos/gerente-de-tienda-cusco-promart-1118500001.html</loc></url>
  <url><loc>https://www.bumeran.com.pe/empleos/arquitecto-a-de-soluciones-de-datos-e-inteligencia-artificial-sector-banca-1118454966.html</loc></url>
  <url><loc>https://www.bumeran.com.pe/empleos/arquitecto-de-datos-senior-azure-indra-peru-1118499000.html</loc></url>
  <url><loc>https://www.bumeran.com.pe/empleos/sub-gerente-de-ti-adecco-peru-s.a.-1118499500.html</loc></url>
</urlset>"""
BUMERAN_COMPANIES = """<urlset>
  <url><loc>https://www.bumeran.com.pe/perfiles/empresa_indra_1.html</loc></url>
  <url><loc>https://www.bumeran.com.pe/perfiles/empresa_indra-peru_2.html</loc></url>
  <url><loc>https://www.bumeran.com.pe/perfiles/empresa_promart_3.html</loc></url>
  <url><loc>https://www.bumeran.com.pe/perfiles/empresa_adecco-peru-s.a._4.html</loc></url>
</urlset>"""


def test_bumeran_sitemap_parse_splits_company_newest_first():
    from jobagent.sources import bumeran

    jobs = bumeran.parse(BUMERAN_JOBS, bumeran.companies(BUMERAN_COMPANIES))
    assert [(j.company, j.title) for j in jobs] == [
        ("Promart", "Gerente de tienda cusco"),
        ("Adecco Peru S.A.", "Sub gerente de ti"),
        ("Indra Peru", "Arquitecto de datos senior azure"),   # longest company slug wins over "indra"
        ("", "Arquitecto a de soluciones de datos e inteligencia artificial sector banca"),  # confidential
    ]
    assert jobs[3].url.endswith("-sector-banca-1118454966.html")
    assert all(j.source == "bumeran" and j.location == "Perú" and j.description for j in jobs)


def test_bumeran_term_match_stems_gender_and_keeps_acronyms_whole():
    from jobagent.sources.bumeran import matches

    terms = ["Gerente de TI", "Arquitecto de Soluciones de Datos", "IA"]
    assert matches("Arquitecto a de soluciones de datos e inteligencia artificial", terms) == terms[1]
    assert matches("Arquitecta de soluciones de datos", terms) == terms[1]
    assert matches("Sub gerente de ti", terms) == terms[0]
    assert matches("Gerente de tienda cusco", terms) is None        # "ti" is not "tienda"
    assert matches("Tech lead de banca digital ia", terms) == "IA"
    assert matches("Ingeniero de datos", terms) is None


def test_bumeran_search_filters_and_caps(monkeypatch):
    from jobagent.sources import bumeran

    xml = {bumeran.JOBS_SITEMAP: BUMERAN_JOBS, bumeran.COMPANIES_SITEMAP: BUMERAN_COMPANIES}
    monkeypatch.setattr(bumeran, "_get", lambda url: xml[url])
    terms = ["Gerente de TI", "Arquitecto de Datos"]
    got = bumeran.search(terms, keep=lambda j: "senior" not in j.title)
    assert [(j.title, j.search_term) for j in got] == [
        ("Sub gerente de ti", "Gerente de TI"),
        ("Arquitecto a de soluciones de datos e inteligencia artificial sector banca", "Arquitecto de Datos")]
    assert len(bumeran.search(terms, max_jobs=1)) == 1


def test_source_bumeran_is_optional_and_never_raises(monkeypatch):
    from jobagent import graph

    assert graph.source_bumeran({"config": {}, "options": {}}) == {"raw": []}
    cfg = {"bumeran": {"extra_terms": ["IA"]}, "jobspy": {"terms": ["CTO"]}, "prefilter": {
        "title_include": ["."], "title_exclude": ["^$"], "geo_keep": ["."]}}
    assert graph.source_bumeran({"config": cfg, "options": {"skip": ["bumeran"]}}) == {"raw": []}
    seen = {}

    def boom(terms, keep, max_jobs):
        seen["terms"] = terms
        raise RuntimeError("403")
    monkeypatch.setattr(graph.bumeran, "search", boom)
    assert graph.source_bumeran({"config": cfg, "options": {}}) == {"raw": []}
    assert seen["terms"] == ["CTO", "IA"]
