"""LangGraph pipeline.

START ─┬─ source_boards ──┬─ prefilter ─ unseen ─ company_check ─ mark_applied ─┬─ score_one ×N (Send) ─┬─ summarize ─ track ─ remember ─ END
       ├─ source_jobspy ──┤                                                   └─ passthrough ─────────┘
       └─ source_bumeran ─┘

`company_check` drops small/regional IT-services consultancies (LinkedIn company page). `track` prunes board
cards whose posting no longer accepts applications, then turns selected postings into GitHub Issues;
`remember` persists what was seen.
"""
import logging
import operator
import time
from collections import Counter
from datetime import date
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from jobagent import ats, linkedin, scoring
from jobagent.models import Job, ScoredJob
from jobagent.prefilter import Prefilter
from jobagent.sources import boards, bumeran, jobspy_src
from jobagent.state import Context

log = logging.getLogger("jobagent")


class State(TypedDict, total=False):
    config: dict
    ctx: Context
    options: dict                                   # skip, limit, write_state, score
    raw: Annotated[list[Job], operator.add]         # parallel sources append here
    jobs: list[Job]
    candidates: list[ScoredJob]
    scored: Annotated[list[ScoredJob], operator.add]
    stats: Annotated[dict, operator.or_]


class ScoreTask(TypedDict):
    item: ScoredJob
    config: dict
    ctx: Context


def source_boards(state: State) -> dict:
    if "boards" in state["options"].get("skip", []):
        return {"raw": []}
    out = []
    for b in state["config"].get("boards", []):
        try:
            got = boards.PROVIDERS[b["provider"]](b["slug"], b["company"])
            log.info("[%s/%s] %d", b["provider"], b["slug"], len(got))
            out += got
        except Exception as e:
            log.warning("[%s/%s] %s", b["provider"], b["slug"], e)
    return {"raw": out}


def source_jobspy(state: State) -> dict:
    js = state["config"].get("jobspy")
    if not js or "jobspy" in state["options"].get("skip", []):
        return {"raw": []}
    return {"raw": jobspy_src.search(js["terms"], js["locations"], js["sites"], js["hours_old"],
                                     js["per_term"], js.get("sleep", 4.0))}


def source_bumeran(state: State) -> dict:
    """Bumeran Perú (sitemap, title only). Terms default to `jobspy.terms`, plus `bumeran.extra_terms`."""
    bm = state["config"].get("bumeran")
    if not bm or "bumeran" in state["options"].get("skip", []):
        return {"raw": []}
    terms = bm.get("terms") or (state["config"].get("jobspy") or {}).get("terms", [])
    try:
        return {"raw": bumeran.search(terms + bm.get("extra_terms", []), Prefilter(state["config"]["prefilter"]),
                                      bm.get("max_jobs", 100))}
    except Exception as e:  # a blocked or failed download must not sink the other sources
        log.warning("[bumeran] %s", e)
        return {"raw": []}


def prefilter(state: State) -> dict:
    pf = Prefilter(state["config"]["prefilter"])
    seen, kept, why = set(), [], Counter()
    for j in state["raw"]:
        if not pf.title_ok(j):
            why["title"] += 1
        elif not pf.type_ok(j):
            why["job_type"] += 1
        elif not pf.company_ok(j):
            why["company"] += 1
        elif not pf.geo_ok(j):
            why["geo"] += 1
        elif j.key in seen or j.url in seen:
            why["dup"] += 1
        else:
            seen |= {j.key, j.url}
            kept.append(j)
    return {"jobs": kept, "stats": {"raw": len(state["raw"]), "prefiltered": len(kept), "dropped": dict(why)}}


def unseen(state: State) -> dict:
    """Drop postings already processed in previous runs (state/seen.jsonl in the data repo)."""
    new = [j for j in state["jobs"] if not state["ctx"].is_seen(j)]
    if limit := state["options"].get("limit"):
        new = new[:limit]
    return {"jobs": new, "stats": {"new": len(new)}}


def company_check(state: State) -> dict:
    """criteria §7: drop IT-services/staffing consultancies headquartered in LATAM below the size threshold."""
    cfg = state["config"].get("company_filter")
    if not cfg or "company_check" in state["options"].get("skip", []):
        return {}
    cache, kept, dropped = {}, [], []
    for j in state["jobs"]:
        if j.company_url and j.company_url not in cache:
            cache[j.company_url] = linkedin.company(j.company_url)
            time.sleep(cfg.get("sleep", 1.0))
        info = cache.get(j.company_url) if j.company_url else None
        if linkedin.local_consultancy(info, cfg):
            dropped.append(f"{j.company} ({info['size']}, {info['country']})")
        else:
            kept.append(j)
    if dropped:
        log.info("company_check dropped: %s", "; ".join(sorted(set(dropped))))
    return {"jobs": kept, "stats": {"consultancy_dropped": len(dropped)}}


def mark_applied(state: State) -> dict:
    """History match, plus the company pause (criteria §13): after N cold applications there that were rejected
    or got no answer, the company is skipped until a referral is listed in config `company_pause.referrals`."""
    ctx, pause = state["ctx"], state["config"].get("company_pause") or {}
    items = []
    for j in state["jobs"]:
        same, others = ctx.applied_match(j)
        paused = ctx.paused(j, pause.get("after", 2), pause.get("referrals", [])) if pause else None
        items.append(ScoredJob(job=j, applied_match=same, company_history=others, paused=paused))
    return {"candidates": items, "stats": {"already_applied": sum(bool(i.applied_match) for i in items),
                                           "paused": sum(bool(i.paused) for i in items)}}


def fan_out(state: State):
    todo = ([i for i in state["candidates"] if not i.applied_match and not i.paused]
            if state["options"].get("score", True) else [])
    done = [i for i in state["candidates"] if i not in todo]
    sends = [Send("score_one", {"item": i, "config": state["config"], "ctx": state["ctx"]}) for i in todo]
    sends.append(Send("passthrough", {"items": done}))
    return sends


def passthrough(task: dict) -> dict:
    return {"scored": task["items"]}


def score_one(task: ScoreTask) -> dict:
    item, cfg, ctx = task["item"], task["config"]["scoring"], task["ctx"]
    try:
        s = scoring.score(item.job, ctx.criteria)
    except Exception as e:
        log.warning("score failed %s: %s", item.job.url, e)
        return {"scored": [item.model_copy(update={"error": f"{type(e).__name__}: {e}"[:300]})]}
    capped = s.requires_operator_leadership or s.internal_bank_role
    total = scoring.weighted(s.dims, cfg["weights"], capped, cfg["operator_cap"])
    cov, missing = ats.coverage([k.model_dump() for k in s.keywords], ctx.cv[s.lang])
    return {"scored": [item.model_copy(update={"score": total, "band": scoring.band(total), "ats_before": cov,
                                               "ats_missing": missing, "llm": s.model_dump()})]}


def track(state: State) -> dict:
    """Create an Issue + Project item per selected posting and post the daily digest."""
    opts = state["options"]
    if not opts.get("track"):
        return {}
    from jobagent.tracker import Tracker, select

    cfg = state["config"]["tracker"]
    t = Tracker(opts["github_token"], opts["repo"], cfg["org"], cfg["project_number"], opts.get("issues_token"))
    pruned = t.prune_closed(cfg.get("prune_statuses", ["Nueva", "Evaluada", "CV listo"]))
    picked = select(state["scored"], cfg)
    lines, created = [], 0
    for s, watch in picked:
        try:
            url = t.create(s, watch)
            created += 1
            lines.append(f"- **{s.score}** · ATS {s.ats_before}% · [{s.job.company} — {s.job.title}]({url})"
                         + (" · 👀 watch" if watch else ""))
        except Exception as e:
            log.warning("issue failed for %s: %s", s.job.url, e)
    t.digest(lines, {k: state["stats"].get(k) for k in ("raw", "prefiltered", "new", "paused", "bands")}, pruned)
    return {"stats": {"issues": created, "pruned": len(pruned)}}


def remember(state: State) -> dict:
    if state["options"].get("write_state"):
        # errored postings are not remembered, so the next run retries them; paused ones neither, so a
        # referral added later lets them through
        state["ctx"].remember([i.job for i in state["scored"] if not i.error and not i.paused],
                              date.today().isoformat())
    return {}


def summarize(state: State) -> dict:
    bands = Counter(i.band or ("applied" if i.applied_match else "paused" if i.paused else "error")
                    for i in state["scored"])
    return {"stats": {"bands": dict(bands)}}


def build():
    g = StateGraph(State)
    for name, fn in [("source_boards", source_boards), ("source_jobspy", source_jobspy),
                     ("source_bumeran", source_bumeran), ("prefilter", prefilter),
                     ("unseen", unseen), ("company_check", company_check), ("mark_applied", mark_applied), ("score_one", score_one),
                     ("passthrough", passthrough), ("summarize", summarize), ("track", track),
                     ("remember", remember)]:
        g.add_node(name, fn)
    g.add_edge(START, "source_boards")
    g.add_edge(START, "source_jobspy")
    g.add_edge(START, "source_bumeran")
    g.add_edge(["source_boards", "source_jobspy", "source_bumeran"], "prefilter")
    g.add_edge("prefilter", "unseen")
    g.add_edge("unseen", "company_check")
    g.add_edge("company_check", "mark_applied")
    g.add_conditional_edges("mark_applied", fan_out, ["score_one", "passthrough"])
    g.add_edge("score_one", "summarize")
    g.add_edge("passthrough", "summarize")
    g.add_edge("summarize", "track")
    g.add_edge("track", "remember")
    g.add_edge("remember", END)
    return g.compile()
