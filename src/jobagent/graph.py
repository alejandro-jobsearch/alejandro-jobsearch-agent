"""LangGraph pipeline.

Phase 1 (this file today):  START ─┬─ source_boards ─┬─ prefilter ─ END
                                   └─ source_jobspy ─┘
Later phases add nodes after `prefilter`: ats_before → llm_score → route → create_issue → digest.
"""
import logging
import operator
from collections import Counter
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from jobagent.models import Job
from jobagent.prefilter import Prefilter
from jobagent.sources import boards, jobspy_src

log = logging.getLogger("jobagent")


class State(TypedDict, total=False):
    config: dict
    skip: list[str]
    raw: Annotated[list[Job], operator.add]      # parallel sources append here
    jobs: list[Job]
    stats: dict


def source_boards(state: State) -> dict:
    if "boards" in state.get("skip", []):
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
    if not js or "jobspy" in state.get("skip", []):
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
    stats = {"raw": len(state["raw"]), "kept": len(kept), "dropped": dict(why),
             "by_source": dict(Counter(j.source for j in kept))}
    return {"jobs": kept, "stats": stats}


def build():
    g = StateGraph(State)
    g.add_node("source_boards", source_boards)
    g.add_node("source_jobspy", source_jobspy)
    g.add_node("prefilter", prefilter)
    g.add_edge(START, "source_boards")
    g.add_edge(START, "source_jobspy")
    g.add_edge(["source_boards", "source_jobspy"], "prefilter")
    g.add_edge("prefilter", END)
    return g.compile()
