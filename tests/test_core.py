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
