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
