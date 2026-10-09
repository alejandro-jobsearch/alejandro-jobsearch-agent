"""LangGraph pipeline.

START ─┬─ source_boards ─┬─ prefilter ─ unseen ─ mark_applied ─┬─ score_one ×N (Send, parallel) ─┬─ summarize ─ track ─ remember ─ END
       └─ source_jobspy ─┘                                     └─ passthrough (already applied) ─┘

`track` turns selected postings into GitHub Issues on the Project board; `remember` persists what was seen.
"""
import logging
import operator
from collections import Counter
from datetime import date
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from jobagent import ats, scoring
from jobagent.models import Job, ScoredJob
from jobagent.prefilter import Prefilter
from jobagent.sources import boards, jobspy_src
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


def prefilter(state: State) -> dict:
    pf = Prefilter(state["config"]["prefilter"])
    seen, kept, why = set(), [], Counter()
    for j in state["raw"]:
        if not pf.title_ok(j):
            why["title"] += 1
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


def mark_applied(state: State) -> dict:
    ctx = state["ctx"]
    items = []
    for j in state["jobs"]:
        same, others = ctx.applied_match(j)
        items.append(ScoredJob(job=j, applied_match=same, company_history=others))
    return {"candidates": items, "stats": {"already_applied": sum(bool(i.applied_match) for i in items)}}


def fan_out(state: State):
    todo = [i for i in state["candidates"] if not i.applied_match] if state["options"].get("score", True) else []
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
    total = scoring.weighted(s.dims, cfg["weights"], s.requires_operator_leadership, cfg["operator_cap"])
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
    t = Tracker(opts["github_token"], opts["repo"], cfg["org"], cfg["project_number"])
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
    t.digest(lines, {k: state["stats"].get(k) for k in ("raw", "prefiltered", "new", "bands")})
    return {"stats": {"issues": created}}


def remember(state: State) -> dict:
    if state["options"].get("write_state"):
        # errored postings are not remembered, so the next run retries them
        state["ctx"].remember([i.job for i in state["scored"] if not i.error], date.today().isoformat())
    return {}


def summarize(state: State) -> dict:
    bands = Counter(i.band or ("applied" if i.applied_match else "error") for i in state["scored"])
    return {"stats": {"bands": dict(bands)}}


def build():
    g = StateGraph(State)
    for name, fn in [("source_boards", source_boards), ("source_jobspy", source_jobspy), ("prefilter", prefilter),
                     ("unseen", unseen), ("mark_applied", mark_applied), ("score_one", score_one),
                     ("passthrough", passthrough), ("summarize", summarize), ("track", track),
                     ("remember", remember)]:
        g.add_node(name, fn)
    g.add_edge(START, "source_boards")
    g.add_edge(START, "source_jobspy")
    g.add_edge(["source_boards", "source_jobspy"], "prefilter")
    g.add_edge("prefilter", "unseen")
    g.add_edge("unseen", "mark_applied")
    g.add_conditional_edges("mark_applied", fan_out, ["score_one", "passthrough"])
    g.add_edge("score_one", "summarize")
    g.add_edge("passthrough", "summarize")
    g.add_edge("summarize", "track")
    g.add_edge("track", "remember")
    g.add_edge("remember", END)
    return g.compile()
