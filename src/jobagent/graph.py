"""LangGraph pipeline.

START ─┬─ source_boards ──┬─ prefilter ─ unseen ─ company_check ─ mark_applied ─┬─ score_one ×N (Send) ─┬─ summarize ─ track ─ remember ─ END
       ├─ source_jobspy ──┤                                                   └─ passthrough ─────────┘
       ├─ source_remote ──┤
       ├─ source_exa ─────┤
       └─ source_bumeran ─┘

`company_check` drops small/regional IT-services consultancies (LinkedIn company page). Bumeran postings carry
only a title, so they skip scoring (passthrough) and go to the digest's triage list, from where the manual
intake form scores the full posting. `track` prunes board cards whose posting no longer accepts applications,
then turns selected postings into GitHub Issues; `remember` persists what was seen and the Bumeran watermark.
`source_exa` is a web search that only runs every `exa_search.every_days` days.
"""
import logging
import os
import operator
import time
from collections import Counter
from datetime import date
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from jobagent import ats, linkedin, scoring
from jobagent.models import Job, ScoredJob, _norm
from jobagent.prefilter import Prefilter
from jobagent.sources import boards, bumeran, exa, jobspy_src, remote
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


def all_boards(config: dict, ctx: Context | None) -> list[dict]:
    """Configured boards plus those discovered from good postings (state/boards.json), without duplicates."""
    out, keys = [], set()
    for b in list(config.get("boards", [])) + (ctx.found_boards() if ctx else []):
        k = (b["provider"], b["slug"].lower())
        if k not in keys:
            keys.add(k)
            out.append(b)
    return out


def discovered_boards(scored: list[ScoredJob], config: dict, ctx: Context) -> list[dict]:
    """Boards behind postings that scored at least `tracker.watch_min` and are not configured yet: a good role on a
    company's Greenhouse/Lever/Ashby/Workday says that company is worth reading every day."""
    floor = (config.get("tracker") or {}).get("watch_min", 60)
    known = {(b["provider"], b["slug"].lower()) for b in all_boards(config, ctx)}
    out = []
    for s in scored:
        if s.score is None or s.score < floor:
            continue
        b = boards.board_from_url(s.job.url) or boards.board_from_url(s.job.apply_url)
        if b and (b["provider"], b["slug"].lower()) not in known:
            known.add((b["provider"], b["slug"].lower()))
            if b["provider"] == "workday":
                b["country"] = (config.get("discovery") or {}).get("workday_country", "peru")
            out.append({**b, "company": s.job.company, "from": s.job.url})
    return out


def source_boards(state: State) -> dict:
    if "boards" in state["options"].get("skip", []):
        return {"raw": []}
    out, keep = [], Prefilter(state["config"]["prefilter"]).title_ok
    for b in all_boards(state["config"], state.get("ctx")):
        extra = {k: v for k, v in b.items() if k not in ("provider", "slug", "company", "found", "from")}
        if b["provider"] in boards.FILTERED:
            extra["keep"] = keep
        try:
            got = boards.PROVIDERS[b["provider"]](b["slug"], b["company"], **extra)
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
                                     js["per_term"], js.get("sleep", 4.0),
                                     max_minutes=js.get("max_minutes"))}


def source_remote(state: State) -> dict:
    """Public remote-job boards (`remote_boards` in config)."""
    cfg = state["config"].get("remote_boards")
    if not cfg or "remote" in state["options"].get("skip", []):
        return {"raw": []}
    return {"raw": remote.search(cfg)}


def source_exa(state: State) -> dict:
    """Web search for postings that no board carries. Every `every_days` days: its date lives in state/exa.json."""
    cfg = state["config"].get("exa_search")
    key = os.environ.get("EXA_API_KEY")
    if not cfg or not key or "exa" in state["options"].get("skip", []):
        return {"raw": []}
    ctx = state.get("ctx")
    last = ctx.exa_last if ctx else None
    if last and (date.today() - date.fromisoformat(last)).days < cfg.get("every_days", 7):
        return {"raw": []}
    try:
        return {"raw": exa.search(cfg, key, Prefilter(state["config"]["prefilter"]).title_ok),
                "stats": {"exa_ran": True}}
    except Exception as e:  # a failed search must not sink the other sources; the next run retries it
        log.warning("[exa] %s", e)
        return {"raw": []}


def source_bumeran(state: State) -> dict:
    """Bumeran Perú (sitemap, title only), postings newer than the stored watermark. Terms default to
    `jobspy.terms`, plus `bumeran.extra_terms`."""
    bm = state["config"].get("bumeran")
    if not bm or not bm.get("enabled", True) or "bumeran" in state["options"].get("skip", []):
        return {"raw": []}
    terms = bm.get("terms") or (state["config"].get("jobspy") or {}).get("terms", [])
    try:
        ctx = state.get("ctx")
        jobs, last_id = bumeran.search(terms + bm.get("extra_terms", []), Prefilter(state["config"]["prefilter"]),
                                       bm.get("max_jobs", 25), ctx.bumeran_last_id if ctx else 0,
                                       bm.get("companies", []))
        jobs = bumeran.enrich(jobs, os.environ.get("EXA_API_KEY"))
        return {"raw": jobs, "stats": {"bumeran_last_id": last_id}}
    except Exception as e:  # a blocked or failed download must not sink the other sources
        log.warning("[bumeran] %s", e)
        return {"raw": []}


def prefilter(state: State) -> dict:
    pf = Prefilter(state["config"]["prefilter"])
    seen, kept, why, rescued = set(), [], Counter(), 0
    for j in state["raw"]:
        if not pf.title_ok(j):
            why["title"] += 1
        elif not pf.type_ok(j):
            why["job_type"] += 1
        elif not pf.company_ok(j):
            why["company"] += 1
        elif not pf.geo_ok(j):
            why["geo"] += 1
        elif not pf.eligible_ok(j):
            why["eligibility"] += 1
        elif j.key in seen or j.url in seen:
            why["dup"] += 1
        else:
            seen |= {j.key, j.url}
            kept.append(j)
            rescued += pf.rescued(j) and not pf.geo_keep.search(j.location or "")
    return {"jobs": kept, "stats": {"raw": len(state["raw"]), "prefiltered": len(kept), "dropped": dict(why),
                                    "geo_rescued": rescued}}


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
        # web-search copies filed under the aggregator's name: use the employer the page names, if known
        if j.source == "exa" and j.company == exa.host_company(j.url) and (real := ctx.employer_in(j.description)) \
                and ctx.canon(real) != ctx.canon(j.company):
            j = j.model_copy(update={"company": real})
        same, others = ctx.applied_match(j)
        paused = ctx.paused(j, pause.get("after", 2), pause.get("referrals", [])) if pause else None
        items.append(ScoredJob(job=j, applied_match=same, company_history=others, paused=paused))
    return {"candidates": items, "stats": {"already_applied": sum(bool(i.applied_match) for i in items),
                                           "paused": sum(bool(i.paused) for i in items)}}


def title_only(j: Job) -> bool:
    """No description to judge it by (Bumeran sitemap, Michael Page blocked): triage list, not scoring."""
    return j.description in (bumeran.NO_DESCRIPTION, boards.MP_NO_DESCRIPTION)


def fan_out(state: State):
    todo = ([i for i in state["candidates"] if not i.applied_match and not i.paused and not title_only(i.job)]
            if state["options"].get("score", True) else [])
    done = [i.model_copy(update={"band": "triage"}) if title_only(i.job) and not i.applied_match and not i.paused
            else i for i in state["candidates"] if i not in todo]
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
    total = scoring.total(s, cfg)
    cov, missing = ats.coverage([k.model_dump() for k in s.keywords], ctx.cv[s.lang])
    return {"scored": [item.model_copy(update={"score": total, "band": scoring.band(total), "ats_before": cov,
                                               "ats_missing": missing, "llm": s.model_dump()})]}


def track(state: State) -> dict:
    """Create an Issue + Project item per selected posting and post the daily digest."""
    opts = state["options"]
    if not opts.get("track"):
        return {}
    from jobagent.tracker import Tracker, intake_link, select, watch_re, watch_title_re

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
    # title-only postings, watched first. The company list is matched on the company only ("aws"/"sap" in a title
    # is a skill, not AWS); `watch_titles` are role families, so those are matched on the title.
    watch, watch_title = watch_re(cfg), watch_title_re(cfg)
    triage = sorted(((s, bool((watch and watch.search(_norm(s.job.company)))
                              or (watch_title and watch_title.search(_norm(s.job.title)))))
                     for s in state["scored"] if s.band == "triage"), key=lambda t: not t[1])
    triage_lines = [f"- [{s.job.title}]({s.job.url}) — {s.job.company or 'empresa no indicada'}"
                    + (" · 👀" if w else "") + f" · [evaluar]({intake_link(opts['repo'], s.job)})"
                    for s, w in triage]
    t.digest(lines, {k: state["stats"].get(k) for k in ("raw", "prefiltered", "new", "paused", "bands")}, pruned,
             triage_lines)
    return {"stats": {"issues": created, "pruned": len(pruned)}}


def remember(state: State) -> dict:
    if state["options"].get("write_state"):
        # errored postings are not remembered, so the next run retries them; paused ones neither, so a
        # referral added later lets them through
        state["ctx"].remember([i.job for i in state["scored"] if not i.error and not i.paused],
                              date.today().isoformat())
        if last_id := state["stats"].get("bumeran_last_id"):
            state["ctx"].save_bumeran(last_id, date.today().isoformat())
        if state["stats"].get("exa_ran"):
            state["ctx"].save_exa(date.today().isoformat())
        if (state["config"].get("discovery") or {}).get("enabled", True):
            new = state["ctx"].add_boards(discovered_boards(state["scored"], state["config"], state["ctx"]),
                                          date.today().isoformat())
            for b in new:
                log.info("board discovered: %s/%s (%s)", b["provider"], b["slug"], b["company"])
    return {}


def summarize(state: State) -> dict:
    bands = Counter(i.band or ("applied" if i.applied_match else "paused" if i.paused else "error")
                    for i in state["scored"])
    return {"stats": {"bands": dict(bands)}}


def build():
    g = StateGraph(State)
    for name, fn in [("source_boards", source_boards), ("source_jobspy", source_jobspy),
                     ("source_remote", source_remote), ("source_exa", source_exa), ("source_bumeran", source_bumeran),
                     ("prefilter", prefilter),
                     ("unseen", unseen), ("company_check", company_check), ("mark_applied", mark_applied), ("score_one", score_one),
                     ("passthrough", passthrough), ("summarize", summarize), ("track", track),
                     ("remember", remember)]:
        g.add_node(name, fn)
    g.add_edge(START, "source_boards")
    g.add_edge(START, "source_jobspy")
    g.add_edge(START, "source_remote")
    g.add_edge(START, "source_exa")
    g.add_edge(START, "source_bumeran")
    g.add_edge(["source_boards", "source_jobspy", "source_remote", "source_exa", "source_bumeran"], "prefilter")
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
