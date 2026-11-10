from datetime import date

from jobagent import ats, scoring
from jobagent.models import Job, ScoredJob
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


def test_tracker_select_watch_titles():
    from jobagent.models import ScoredJob
    from jobagent.tracker import select

    mk = lambda title, score: ScoredJob(job=Job(source="x", company="Acme", title=title, url=title), score=score)
    cfg = {"min_score": 75, "watch_min": 60, "watch_titles": [r"\b(jefe|gerente)\b.{0,40}\b(transformacion|tecnologia)"]}
    picked = select([mk("Jefe de Transformación Digital", 65), mk("Gerente de Tecnología", 74),
                     mk("Jefe de Tecnología", 59), mk("Data Lead", 70)], cfg)
    assert [(s.job.title, w) for s, w in picked] == [("Gerente de Tecnología", True),
                                                     ("Jefe de Transformación Digital", True)]


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


def test_bumeran_term_match_is_a_phrase_not_a_bag_of_words():
    from jobagent.sources.bumeran import matches

    terms = ["Gerente de Tecnología", "Arquitecto de Datos", "Gerente de TI", "Arquitecto de Soluciones"]
    assert matches("Gerente de negocios canal tecnologia equipos moviles", terms) is None   # words apart
    assert matches("Gestor de proyectos de migracion de datos arquitecturas distribuidas", terms) is None
    assert matches("Gerente de proyectos ti product manager", terms) is None
    assert matches("Gerente de tecnologias de informacion", terms) == terms[0]              # plural folded
    assert matches("Arquitecto senior de soluciones on premise aws", terms) == terms[3]      # seniority filler
    assert matches("Arquitecta de datos", terms) == terms[1]


def test_bumeran_parse_knows_recruiters_and_extra_companies():
    from jobagent.sources import bumeran

    xml = """<urlset>
  <url><loc>https://www.bumeran.com.pe/empleos/arquitecto-de-soluciones-experis-peru-1118000003.html</loc></url>
  <url><loc>https://www.bumeran.com.pe/empleos/ia-business-specialist-senior-adecco-bcp-1118000002.html</loc></url>
  <url><loc>https://www.bumeran.com.pe/empleos/data-architect-acme-labs-1118000001.html</loc></url>
</urlset>"""
    jobs = bumeran.parse(xml, ["acme-labs"])
    assert [(j.company, j.title) for j in jobs] == [
        ("Experis Peru", "Arquitecto de soluciones"),
        ("Adecco Bcp", "Ia business specialist senior"),     # "adecco-bcp" wins over "bcp"
        ("Acme Labs", "Data architect")]


def test_bumeran_search_filters_caps_and_keeps_only_new_ids(monkeypatch):
    from jobagent.sources import bumeran

    xml = {bumeran.JOBS_SITEMAP: BUMERAN_JOBS, bumeran.COMPANIES_SITEMAP: BUMERAN_COMPANIES}
    monkeypatch.setattr(bumeran, "_get", lambda url: xml[url])
    terms = ["Gerente de TI", "Arquitecto de Datos", "Arquitecto de Soluciones de Datos"]
    got, last_id = bumeran.search(terms, keep=lambda j: "senior" not in j.title)
    assert [(j.title, j.search_term) for j in got] == [
        ("Sub gerente de ti", "Gerente de TI"),
        ("Arquitecto a de soluciones de datos e inteligencia artificial sector banca",
         "Arquitecto de Soluciones de Datos")]
    assert last_id == 1118500001                              # highest id in the sitemap, matched or not
    assert len(bumeran.search(terms, max_jobs=1)[0]) == 1
    got, _ = bumeran.search(terms, after_id=1118499000)
    assert [j.title for j in got] == ["Sub gerente de ti"]
    assert bumeran.search(terms, after_id=1118600000) == ([], 1118600000)   # watermark never goes back


def test_title_only_postings_skip_scoring_and_go_to_triage():
    from jobagent import graph
    from jobagent.sources import bumeran

    bum = ScoredJob(job=Job(source="bumeran", company="Indra Peru", title="Arquitecto de datos",
                            url="https://www.bumeran.com.pe/empleos/x-1.html", description=bumeran.NO_DESCRIPTION))
    li = ScoredJob(job=Job(source="linkedin", company="Acme", title="CTO", url="https://x/2", description="desc"))
    sends = graph.fan_out({"candidates": [bum, li], "options": {}, "config": {}, "ctx": None})
    assert [s.arg["item"].job.source for s in sends if s.node == "score_one"] == ["linkedin"]
    assert [i.band for i in sends[-1].arg["items"]] == ["triage"]


BUMERAN_TEXT = """Jefe de TI Mina - noviembre 2026 | Bumeran Perú

# Jefe de TI Mina

## Publicado hace 10 minutos

## Lima, Lima, Peru

### Descripción del puesto

Liderar el área de tecnología de la organización, su presupuesto, el equipo y los proveedores, con foco en continuidad
operativa, gobierno de TI y la implementación de la plataforma cloud corporativa.

- Remoto
- Tecnologia / Sistemas

Publicado el 03/11/2026

Ingresa tu sueldo bruto pretendido y calculamos un aproximado

Empresa de 11 a 50 empleados"""


def test_bumeran_details_reads_the_posting_and_drops_the_page_tail():
    from jobagent.sources import bumeran

    d = bumeran.details(BUMERAN_TEXT)
    assert d["date_posted"] == date(2026, 11, 3) and d["location"] == "Lima, Lima, Peru" and d["remote"] is True
    assert "gobierno de TI" in d["description"] and "empleados" not in d["description"]
    assert bumeran.details("Just a moment... Enable JavaScript and cookies to continue") == {}
    assert bumeran.details("### Descripción del puesto\n\ncorto") == {}


def test_bumeran_enrich_keeps_title_only_what_the_index_lacks(monkeypatch):
    import httpx
    from jobagent.sources import bumeran

    jobs = [Job(source="bumeran", company="", title=t, url=f"https://www.bumeran.com.pe/empleos/{t}-1.html",
                description=bumeran.NO_DESCRIPTION) for t in ("a", "b")]
    assert bumeran.enrich(jobs, None) is jobs and bumeran.enrich([], "key") == []

    sent = {}
    def post(url, headers, json, timeout):
        sent.update(url=url, headers=headers, json=json)
        return httpx.Response(200, json={"results": [{"url": jobs[0].url, "text": BUMERAN_TEXT}]},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(bumeran.httpx, "post", post)
    got = bumeran.enrich(jobs, "key")
    assert sent["headers"] == {"x-api-key": "key"} and sent["json"]["urls"] == [j.url for j in jobs]
    assert "gobierno de TI" in got[0].description and got[1].description == bumeran.NO_DESCRIPTION

    def down(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(bumeran.httpx, "post", down)
    assert bumeran.enrich(jobs, "key") is jobs


def test_intake_link_prefills_the_bumeran_form():
    from urllib.parse import parse_qs, urlparse

    from jobagent.tracker import intake_link

    j = Job(source="bumeran", company="Indra Peru", title="Arquitecto de datos",
            url="https://www.bumeran.com.pe/empleos/x-1.html")
    u = urlparse(intake_link("org/data", j))
    q = parse_qs(u.query)
    assert u.path == "/org/data/issues/new" and q["template"] == ["bumeran-intake.yml"]
    assert q["title"] == ["[Bumeran Intake] Arquitecto de datos"] and q["cargo"] == ["Arquitecto de datos"]
    assert q["url"] == [j.url] and q["company"] == ["Indra Peru"]


def test_source_bumeran_is_optional_and_never_raises(monkeypatch):
    from jobagent import graph

    assert graph.source_bumeran({"config": {}, "options": {}}) == {"raw": []}
    cfg = {"bumeran": {"extra_terms": ["IA"]}, "jobspy": {"terms": ["CTO"]}, "prefilter": {
        "title_include": ["."], "title_exclude": ["^$"], "geo_keep": ["."]}}
    assert graph.source_bumeran({"config": cfg, "options": {"skip": ["bumeran"]}}) == {"raw": []}
    seen = {}

    def boom(terms, keep, max_jobs, after_id, companies):
        seen["terms"] = terms
        raise RuntimeError("403")
    monkeypatch.setattr(graph.bumeran, "search", boom)
    assert graph.source_bumeran({"config": cfg, "options": {}}) == {"raw": []}
    assert seen["terms"] == ["CTO", "IA"]


def _score(**kw):
    from jobagent.scoring import LLMScore

    base = {"dims": {"industry": 30, "domain": 80}, "requires_operator_leadership": False, "lane": "D", "lang": "en",
            "english_work": "yes", "seniority_real": "x", "gaps": [], "summary": "s", "keywords": []}
    return LLMScore.model_validate({**base, **kw})


def test_lane_weights_need_a_confirmed_open_remote_role():
    cfg = {"weights": {"industry": 50, "domain": 50}, "operator_cap": 70,
           "lane_weights": {"D": {"industry": 0, "domain": 100}}}
    assert scoring.total(_score(remote_eligibility="peru_ok"), cfg) == 80
    assert scoring.total(_score(remote_eligibility="latam"), cfg) == 80
    assert scoring.total(_score(remote_eligibility="unclear"), cfg) == 55
    assert scoring.total(_score(lane="A", remote_eligibility="peru_ok"), cfg) == 55
    capped = _score(remote_eligibility="peru_ok", internal_bank_role=True, dims={"industry": 0, "domain": 100})
    assert scoring.total(capped, cfg) == 70


def test_restricted_remote_is_a_hard_exclusion():
    s = scoring._apply_remote_rule(_score(remote_eligibility="restricted"))
    assert s.hard_exclusion and s.exclusion_reason
    assert not scoring._apply_remote_rule(_score(remote_eligibility="peru_ok")).hard_exclusion


def test_prefilter_eligibility_only_outside_local_market():
    pf = Prefilter({"title_include": ["architect"], "title_exclude": ["junior"], "geo_keep": ["peru", "worldwide"],
                    "geo_drop": ["."], "geo_local": ["peru"],
                    "eligibility_drop": [r"authorized to work in the (us|united states)"]})
    text = "You must be authorized to work in the United States."
    def mk(loc, desc):
        return Job(source="x", company="c", title="AI Architect", url="u", location=loc, description=desc)
    assert not pf(mk("Worldwide", text))
    assert pf(mk("Worldwide", "Hire anywhere"))
    assert pf(mk("Peru", text))          # published for Peru: the LLM decides


def test_jobspy_location_can_search_a_subset_of_terms(monkeypatch):
    import sys
    import types

    from jobagent.sources import jobspy_src

    calls = []

    class Empty:
        def __len__(self):
            return 0

        def to_dict(self, _):
            return []

    def scrape(**kw):
        calls.append((kw["search_term"], kw["location"]))
        return Empty()

    monkeypatch.setitem(sys.modules, "jobspy", types.SimpleNamespace(scrape_jobs=scrape))
    monkeypatch.setattr(jobspy_src.time, "sleep", lambda _: None)
    jobspy_src.search(["a", "b"], [{"name": "Lima"}, {"name": "Peru", "remote": True, "terms": ["b"]}],
                      ["linkedin"], 24, 5)
    assert calls == [("a", "Lima"), ("b", "Lima"), ("b", "Peru")]


class _Resp:
    def __init__(self, payload=None, content=b""):
        self.payload, self.content = payload, content

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_himalayas_drops_other_time_zones_and_dedupes(monkeypatch):
    from jobagent.sources import remote

    jobs = [{"title": "AI Architect", "companyName": "Acme", "guid": "g1", "locationRestrictions": [],
             "timezoneRestrictions": [-5, -4], "description": "<p>Agents</p>", "pubDate": 1793000000,
             "minSalary": 80000, "maxSalary": 90000, "currency": "USD"},
            {"title": "AI Architect", "companyName": "Far", "guid": "g2", "locationRestrictions": ["Peru"],
             "timezoneRestrictions": [8, 9], "description": "x", "pubDate": 1793000000}]
    monkeypatch.setattr(remote.httpx, "get", lambda *a, **k: _Resp({"jobs": jobs}))
    monkeypatch.setattr(remote.time, "sleep", lambda _: None)
    got = remote.himalayas(["a", "b"], "Peru", -5)
    assert [j.url for j in got] == ["g1"]
    assert got[0].location == "Worldwide" and got[0].description == "Agents" and got[0].salary_max == 90000


def test_weworkremotely_splits_company_and_title(monkeypatch):
    from jobagent.sources import remote

    rss = (b"<rss><channel><item><title>Acme: Head of AI</title><region>Anywhere in the World</region>"
           b"<link>https://wwr/1</link><pubDate>Tue, 03 Nov 2026 10:00:00 +0000</pubDate>"
           b"<description>&lt;p&gt;Lead&lt;/p&gt;</description></item></channel></rss>")
    monkeypatch.setattr(remote.httpx, "get", lambda *a, **k: _Resp(content=rss))
    j = remote.weworkremotely()[0]
    assert (j.company, j.title, j.location, j.description) == ("Acme", "Head of AI", "Anywhere in the World", "Lead")
    assert j.date_posted == date(2026, 11, 3)


def test_getonboard_keeps_fully_remote_and_listed_country(monkeypatch):
    from jobagent.sources import remote

    def row(i, modality, countries):
        return {"id": i, "links": {"public_url": f"https://gob/{i}"}, "attributes": {
            "title": "Head of Data", "remote": True, "remote_modality": modality, "countries": countries,
            "published_at": 1793000000, "functions": "<p>Lead</p>", "max_salary": 7000, "min_salary": 6000,
            "company": {"data": {"attributes": {"name": "Acme"}}}}}

    data = [row("a", "fully_remote", ["Remote"]), row("b", "remote_local", ["Chile"]), row("c", "remote_local", ["Perú"])]
    monkeypatch.setattr(remote.httpx, "get", lambda *a, **k: _Resp({"data": data}))
    monkeypatch.setattr(remote.time, "sleep", lambda _: None)
    got = remote.getonboard(["x"], "per[uú]")
    assert [j.url for j in got] == ["https://gob/a", "https://gob/c"]
    assert got[0].location == "Remote - Latin America" and got[0].salary_max == 7000 and got[0].currency == "USD"


def test_remote_search_isolates_a_failing_source(monkeypatch):
    from jobagent.sources import remote

    def boom():
        raise RuntimeError("blocked")

    old = Job(source="x", company="c", title="t", url="u", date_posted=date(2020, 1, 1))
    new = Job(source="x", company="c", title="t", url="v", date_posted=date.today())
    monkeypatch.setattr(remote, "weworkremotely", boom)
    monkeypatch.setattr(remote, "himalayas", lambda *a: [old, new])
    got = remote.search({"terms": ["a"], "sources": ["weworkremotely", "himalayas"]})
    assert [j.url for j in got] == ["v"]


def test_geo_block_beats_geo_keep():
    pf = Prefilter({"title_include": ["architect"], "title_exclude": ["junior"], "geo_keep": ["peru"], "geo_drop": ["."],
                    "geo_block": [r"\busca\b"]})
    def mk(loc):
        return Job(source="x", company="c", title="AI Architect", url="u", location=loc)
    assert not pf(mk("Usca, La Libertad, Peru"))    # LinkedIn's reading of the "USCA" region
    assert pf(mk("Lima, Peru"))


# --- regional roles, relaunched searches, web search ---------------------------------------------------------
def _rescue_cfg():
    return {"title_include": ["director"], "title_exclude": ["junior"], "geo_keep": ["peru"], "geo_drop": ["."],
            "geo_block": [r"\busca\b"], "geo_rescue_places": ["colombia|mexico"],
            "geo_rescue_text": ["ubicaci[oó]n flexible", r"remot[oa][^.\n]{0,60}latam"]}


def test_regional_role_is_rescued_only_with_the_text_and_the_place():
    pf = Prefilter(_rescue_cfg())
    job = lambda loc, d: Job(source="linkedin", company="Acme", title="Director IA", location=loc, url="u", description=d)
    assert pf.geo_ok(job("Medellín, Colombia", "Latinoamérica | Ubicación flexible en países de la región"))
    assert pf.geo_ok(job("Mexico City, Mexico", "Trabajo remoto para todo LATAM"))
    assert not pf.geo_ok(job("Medellín, Colombia", "Presencial en nuestra sede principal"))
    assert not pf.geo_ok(job("Madrid, Spain", "Ubicación flexible"))


def test_rescue_is_off_without_config():
    cfg = {k: v for k, v in _rescue_cfg().items() if not k.startswith("geo_rescue")}
    j = Job(source="linkedin", company="Acme", title="Director IA", location="Bogotá, Colombia", url="u",
            description="Ubicación flexible")
    assert not Prefilter(cfg).geo_ok(j)


def _ctx(applied):
    from pathlib import Path

    from jobagent.state import Context
    return Context(data_dir=Path("."), criteria="", cv={}, applied=applied)


def test_failed_application_does_not_block_a_new_posting():
    j = Job(source="linkedin", company="Acme", title="Head of AI", url="u")
    same, others = _ctx([{"company": "Acme", "role": "Head of AI", "note": "SIN RESPUESTA, cerrada"}]).applied_match(j)
    assert same is None and others[0].startswith("posible relanzamiento")
    same, _ = _ctx([{"company": "Acme", "role": "Head of AI", "note": "POSTULADO 2026-11-01"}]).applied_match(j)
    assert same


def test_exa_split_title():
    from jobagent.sources.exa import split_title
    u = "https://www.remotefront.com/remote-jobs/x"
    assert split_title("Remote Head of AI Platforms at Tenpo | RemoteFront", u) == ("Head of AI Platforms", "Tenpo")
    assert split_title("[Hiring] Head of Artificial Intelligence @Casa Principal", u) == (
        "Head of Artificial Intelligence", "Casa Principal")
    assert split_title("AI Governance Lead at Keyrus - Colombia Remote", u) == ("AI Governance Lead", "Keyrus")
    assert split_title("Jefe/a de Plataformas de IA - Tenpo", "https://tenpo.teamtailor.com/jobs/1") == (
        "Jefe/a de Plataformas de IA", "Tenpo")
    assert split_title("Director de Datos - Medellín, Colombia", "https://careers.wtwco.com/jobs/x") == (
        "Director de Datos - Medellín, Colombia", "Wtwco")
    assert split_title("Senior Lead", "https://jobs.lever.co/acme-corp/1") == ("Senior Lead", "Acme Corp")
    assert split_title("AI Governance Principal en Blackbaud | Remotos LATAM", u) == (
        "AI Governance Principal", "Blackbaud")
    assert split_title("Especialista en Gobierno", "https://careers.acme.com/1") == ("Especialista en Gobierno", "Acme")


def test_exa_to_job_keeps_only_recent_postings():
    from jobagent.sources.exa import to_job
    text = "Requisitos: experiencia liderando equipos de datos e inteligencia artificial. " * 10
    day = date(2026, 11, 6)
    ok = {"url": "https://acme.com/jobs/1", "title": "Head of AI at Acme", "text": text,
          "publishedDate": "2026-11-01T00:00:00Z"}
    assert to_job(ok, day, 45, "q").company == "Acme"
    assert to_job({**ok, "publishedDate": None}, day, 45, "q").date_posted is None
    assert to_job({**ok, "publishedDate": "2026-08-01T00:00:00Z"}, day, 45, "q") is None
    assert to_job({**ok, "url": "https://linkedin.com/posts/x"}, day, 45, "q") is None
    assert to_job({**ok, "text": "Corto"}, day, 45, "q") is None
    assert to_job({**ok, "text": "Noticia sobre el mercado. " * 30}, day, 45, "q") is None


def test_exa_waits_for_its_interval(monkeypatch):
    from datetime import timedelta

    from jobagent import graph

    calls = []
    monkeypatch.setenv("EXA_API_KEY", "k")
    monkeypatch.setattr(graph.exa, "search", lambda cfg, key, keep: calls.append(1) or [])
    state = {"config": {"exa_search": {"every_days": 7, "queries": ["q"]}, "prefilter": _rescue_cfg()}, "options": {}}
    ctx = _ctx([])
    ctx.exa_last = (date.today() - timedelta(days=3)).isoformat()
    assert graph.source_exa({**state, "ctx": ctx}) == {"raw": []} and not calls
    ctx.exa_last = (date.today() - timedelta(days=7)).isoformat()
    assert graph.source_exa({**state, "ctx": ctx})["stats"] == {"exa_ran": True} and calls


def test_exa_apply_links():
    from jobagent.sources.exa import apply_links
    page = ('<a href="https://jobs.lever.co/acme/123-abc">Apply</a> <a href="https://jobs.lever.co/acme/123-abc">x</a>'
            '<a href="https://acme.factorial.mx/apply/head-of-ai-1">y</a> <a href="https://acme.inhire.app/vagas/1">z</a>')
    assert apply_links(page, "https://acme.com/careers/head") == [
        "https://jobs.lever.co/acme/123-abc", "https://acme.factorial.mx/apply/head-of-ai-1"]
    assert apply_links(page, "https://jobs.lever.co/acme/123-abc") == ["https://acme.factorial.mx/apply/head-of-ai-1"]


def test_exa_is_closed_follows_apply_link(monkeypatch):
    from jobagent import linkedin
    from jobagent.sources import exa

    class R:
        def __init__(self, code, text="", url="https://acme.com/careers/head"):
            self.status_code, self.text, self.url = code, text, url
    monkeypatch.setattr(linkedin, "_get", lambda u: R(200, '<a href="https://jobs.lever.co/acme/1">Apply</a>'))
    monkeypatch.setattr(linkedin, "is_closed", lambda u: True)
    assert exa.is_closed("https://acme.com/careers/head") is True
    monkeypatch.setattr(linkedin, "is_closed", lambda u: False)
    assert exa.is_closed("https://acme.com/careers/head") is False
    monkeypatch.setattr(linkedin, "_get", lambda u: R(404))
    assert exa.is_closed("https://acme.com/careers/head") is True
    monkeypatch.setattr(linkedin, "_get", lambda u: None)
    assert exa.is_closed("https://acme.com/careers/head") is None


class _R:
    def __init__(self, d=None, content=b"", text=""):
        self.d, self.content, self.text = d, content, text
    def raise_for_status(self): pass
    def json(self): return self.d


def test_workday_country_facet_and_details_only_for_kept_titles(monkeypatch):
    from jobagent.sources import boards

    base = "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/Ext"
    facets = [{"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locationCountry", "values": [{"id": "pe1", "descriptor": "Peru"},
                                                         {"id": "us1", "descriptor": "United States"}]}]}]
    posts, fetched = [], []

    def post(url, json, **kw):
        posts.append(json)
        if json["limit"] == 1:
            return _R({"facets": facets, "jobPostings": []})
        return _R({"jobPostings": [{"title": "Head of AI", "externalPath": "/job/Lima/Head_1", "locationsText": "Lima"},
                                   {"title": "Cajero", "externalPath": "/job/Lima/Cajero_2", "locationsText": "Lima"}]})

    def get(url, **kw):
        fetched.append(url)
        return _R({"jobPostingInfo": {"location": "Lima", "additionalLocations": ["Remote"],
                                      "country": {"descriptor": "Peru"}, "jobDescription": "<p>Lead AI</p>",
                                      "startDate": "2026-11-05", "externalUrl": "https://x/Head_1",
                                      "remoteType": "Hybrid"}})

    monkeypatch.setattr(boards.httpx, "post", post)
    monkeypatch.setattr(boards.httpx, "get", get)
    [j] = boards.workday("acme.wd3/Ext", "Acme", country="peru", keep=lambda j: "Head" in j.title, sleep=0)
    assert posts[1]["appliedFacets"] == {"locationCountry": ["pe1"]}
    assert fetched == [base + "/job/Lima/Head_1"]
    assert (j.location, j.description, str(j.date_posted), j.url) == (
        "Lima, Remote, Peru", "Lead AI", "2026-11-05", "https://x/Head_1")

    monkeypatch.setattr(boards.httpx, "post", lambda url, json, **kw: _R({"facets": [], "jobPostings": []}))
    assert boards.workday("acme.wd3/Ext", "Acme", country="peru") == []


def test_successfactors_ands_the_place_and_splits_it_from_the_title(monkeypatch):
    from jobagent.sources import boards

    seen = []
    rss = (b"<rss><channel><item><title>Gerente de Datos (Lima, LIM, PE)</title>"
           b"<link>https://jobs.acme.com/job/1/?feedId=null</link><description>&lt;p&gt;IA&lt;/p&gt;</description>"
           b"<pubDate>Mon, 09 Nov 2026 0:00:00 GMT</pubDate></item></channel></rss>")
    monkeypatch.setattr(boards.httpx, "get", lambda url, params, **kw: seen.append(params["keywords"]) or _R(content=rss))
    [j] = boards.successfactors("jobs.acme.com", "Acme", search=["Gerente", "Datos"], place="LIM")
    assert seen == ["(Gerente) AND (LIM)", "(Datos) AND (LIM)"]
    assert (j.title, j.location, j.url, j.description, str(j.date_posted)) == (
        "Gerente de Datos", "Lima, LIM, PE", "https://jobs.acme.com/job/1/", "IA", "2026-11-09")


def test_amazon_and_michaelpage_parse(monkeypatch):
    from jobagent.sources import boards

    monkeypatch.setattr(boards.httpx, "get", lambda url, **kw: _R({"jobs": [{
        "title": "Technical Account Manager", "company_name": "AWS", "normalized_location": "Lima, PE",
        "job_path": "/en/jobs/1/tam", "posted_date": "September 21, 2026", "description": "<p>Lead</p>",
        "basic_qualifications": "x", "preferred_qualifications": None}]}))
    [a] = boards.amazon("PER", "Amazon")
    assert (a.company, a.url, str(a.date_posted)) == ("AWS", "https://www.amazon.jobs/en/jobs/1/tam", "2026-09-21")

    listing = '<a href="/job-detail/gerente-de-tecnolog%C3%ADa/ref/1">a</a><a href="/job-detail/cajero/ref/2">b</a>'
    detail = ('<script type="application/ld+json">{"@type": "JobPosting", "title": "Gerente de Tecnología",'
              '"datePosted": "2026-11-04", "employmentType": "FULL_TIME", "description": "<p>Roadmap</p>",'
              '"jobLocation": {"address": {"addressLocality": "Lima", "addressCountry": "Perú"}}}</script>')
    got = []
    monkeypatch.setattr(boards.httpx, "get", lambda url, **kw: got.append(url) or _R(text=listing if "/jobs/" in url else detail))
    [m] = boards.michaelpage("www.mp.pe/jobs/tecnologia", "Michael Page", keep=lambda j: "tecnolog" in j.title, sleep=0)
    assert got[1:] == ["https://www.mp.pe/job-detail/gerente-de-tecnolog%C3%ADa/ref/1"]
    assert (m.title, m.location, m.job_type, m.description) == ("Gerente de Tecnología", "Lima, Perú", "fulltime", "Roadmap")


def test_exa_domain_queries_and_computrabajo_titles():
    from jobagent.sources.exa import query_body, to_job

    cfg = {"exclude_domains": ["junk.com"], "num_results": 5}
    q, body = query_body("Head of AI", cfg)
    assert q == "Head of AI" and body["excludeDomains"] == ["junk.com"] and "includeDomains" not in body
    q, body = query_body({"q": "Gerente TI", "domains": ["pe.computrabajo.com"]}, cfg)
    assert body["includeDomains"] == ["pe.computrabajo.com"] and "excludeDomains" not in body

    text = "Funciones: liderar la estrategia de TI. Requerimientos: 8 años. " * 10
    day = date(2026, 11, 9)
    r = {"url": "https://pe.computrabajo.com/ofertas-de-trabajo/oferta-de-trabajo-de-gerente-de-ti-en-miraflores-ABC",
         "title": "Empleo deGerente de Tecnología en Lima en Grupo Expro - Miraflores", "text": text}
    j = to_job(r, day, 30, "q")
    assert (j.title, j.company, j.location) == ("Gerente de Tecnología en Lima", "Grupo Expro", "Miraflores, Perú")
    assert to_job({**r, "url": "https://pe.computrabajo.com/trabajo-de-gerente-de-tecnologia-en-lima"}, day, 30, "q") is None


def test_exa_split_title_drops_every_board_segment():
    from jobagent.sources.exa import split_title
    assert split_title("Senior AI Solutions & Enterprise Architect en 10Pearls · Empleos remotos en LATAM · heyeco",
                       "https://heyeco.app/jobs/1") == ("Senior AI Solutions & Enterprise Architect", "10Pearls")
    assert split_title("AI Governance Lead – Remote Job at Keyrus Colombia | Find My Remote | Jobs",
                       "https://findmyremote.ai/x") == ("AI Governance Lead", "Keyrus Colombia")


def test_company_aliases_meet_the_history(tmp_path):
    ctx = _ctx([{"company": "WTW", "role": "Director de Data Strategy e Inteligencia Artificial LATAM",
                 "note": "DESCARTADA 2026-11-07"},
                {"company": "phData", "role": "Applied AI Solutions Architect", "note": "DESCARTADA"}])
    ctx.aliases = {"WTW": ["Willis Towers Watson"], "phData": ["Ph Data"]}
    copy = Job(source="exa", company="윌리스타워스왓슨(Willis Towers Watson)", url="u",
               title="Director de Data Strategy e Inteligencia Artificial LATAM")
    assert ctx.applied_match(copy)[0].startswith("WTW")
    assert ctx.applied_match(Job(source="exa", company="Ph Data in LATAM", url="u",
                                 title="Applied AI solutions Architect"))[0].startswith("phData")
    assert ctx.canon("Acme Data") == "acme data"


def test_exa_apply_links_follow_teamtailor_from_a_copy():
    from jobagent.sources.exa import apply_links
    page = '<a href="https://knowmadmood.teamtailor.com/jobs/8437310-ai-architect-latam\\">Apply</a>'
    assert apply_links(page, "https://applypick.com/x") == ["https://knowmadmood.teamtailor.com/jobs/8437310-ai-architect-latam"]


def test_board_from_url():
    from jobagent.sources.boards import board_from_url
    assert board_from_url("https://jobs.lever.co/acme-co/123-abc") == {"provider": "lever", "slug": "acme-co"}
    assert board_from_url("https://job-boards.greenhouse.io/acme/jobs/1") == {"provider": "greenhouse", "slug": "acme"}
    assert board_from_url("https://jobs.ashbyhq.com/acme/abc") == {"provider": "ashby", "slug": "acme"}
    assert board_from_url("https://acme.wd3.myworkdayjobs.com/en-US/Ext/job/Lima/X_1") == {
        "provider": "workday", "slug": "acme.wd3/Ext"}
    assert board_from_url("https://job-boards.eu.greenhouse.io/finnapp/jobs/1") is None
    assert board_from_url("https://www.linkedin.com/jobs/view/1") is None and board_from_url(None) is None


def test_discovered_boards_from_good_postings_only(tmp_path):
    from jobagent import graph
    from jobagent.state import Context

    ctx = Context(data_dir=tmp_path, criteria="", cv={}, applied=[])
    cfg = {"boards": [{"provider": "lever", "slug": "known", "company": "Known"}], "tracker": {"watch_min": 60}}
    mk = lambda url, score, apply=None: ScoredJob(job=Job(source="exa", company="Acme", title="Head of AI", url=url,
                                                          apply_url=apply), score=score)
    scored = [mk("https://jobs.lever.co/acme/1", 80), mk("https://jobs.lever.co/known/2", 90),
              mk("https://jobs.lever.co/low/3", 40), mk("https://www.linkedin.com/jobs/view/9", 70,
                                                         "https://acme2.wd5.myworkdayjobs.com/Careers/job/x")]
    found = graph.discovered_boards(scored, cfg, ctx)
    assert [(b["provider"], b["slug"]) for b in found] == [("lever", "acme"), ("workday", "acme2.wd5/Careers")]
    assert found[1]["country"] == "peru"
    assert len(ctx.add_boards(found, "2026-11-09")) == 2 and ctx.add_boards(found, "2026-11-10") == []
    assert [b["slug"] for b in graph.all_boards(cfg, ctx)] == ["known", "acme", "acme2.wd5/Careers"]


def test_jobicy_parses(monkeypatch):
    from jobagent.sources import remote

    class R:
        def raise_for_status(self): pass
        def json(self): return {"jobs": [{"jobTitle": "Head of AI &amp; Data", "companyName": "Acme", "jobGeo": "LATAM",
                                          "url": "https://jobicy.com/jobs/1", "jobDescription": "<p>Lead</p>",
                                          "pubDate": "2026-11-08 10:00:00"}]}
    monkeypatch.setattr(remote.httpx, "get", lambda *a, **k: R())
    [j] = remote.jobicy()
    assert (j.title, j.location, j.description, str(j.date_posted)) == ("Head of AI & Data", "LATAM", "Lead", "2026-11-08")


def test_michaelpage_blocked_detail_goes_to_triage(monkeypatch):
    import httpx

    from jobagent import graph
    from jobagent.sources import boards

    listing = '<a href="/job-detail/gerente-de-ti/ref/1?ng-src=x&amp;ng-jn=y">a</a>'

    def get(url, **kw):
        if "/jobs/" in url:
            return _R(text=listing)
        resp = httpx.Response(403, request=httpx.Request("GET", url))
        raise httpx.HTTPStatusError("403", request=resp.request, response=resp)
    monkeypatch.setattr(boards.httpx, "get", get)
    [j] = boards.michaelpage("www.mp.pe/jobs/tecnologia", "Michael Page", sleep=0)
    assert (j.title, j.url) == ("Gerente de ti", "https://www.mp.pe/job-detail/gerente-de-ti/ref/1")
    assert graph.title_only(j)
