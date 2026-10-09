"""Run the pipeline.

    python -m jobagent.cli --data-dir ../alejandro-jobsearch-data --out out
        [--skip jobspy boards] [--input out/jobs.jsonl] [--limit 10] [--no-score] [--write-state]
        [--max-concurrency 6]

--input reuses a previous sourcing run (skips both sources) — handy to iterate on scoring.
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

import yaml

from jobagent.graph import build, track
from jobagent.models import Job, ScoredJob
from jobagent.state import Context


def report(scored, stats) -> str:
    rows = sorted(scored, key=lambda i: (i.applied_match is not None, -(i.score or -1)))
    lines = [f"# Corrida — {stats}", "",
             "| Score | Banda | ATS antes | Lane | Empresa | Rol | Ubicación | Nota |", "|---|---|---|---|---|---|---|---|"]
    for i in rows:
        llm = i.llm or {}
        note = f"YA POSTULADO: {i.applied_match}" if i.applied_match else (i.error or llm.get("summary", ""))
        if i.company_history:
            note += f" · Misma empresa: {'; '.join(i.company_history)}"
        lines.append(f"| {i.score if i.score is not None else '–'} | {i.band or '–'} | "
                     f"{i.ats_before if i.ats_before is not None else '–'} | {llm.get('lane', '–')} | {i.job.company} | "
                     f"[{i.job.title}]({i.job.url}) | {i.job.location} | {' '.join(note.replace('|', '/').split())[:400]} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--skip", nargs="*", default=[])
    p.add_argument("--input")
    p.add_argument("--limit", type=int)
    p.add_argument("--no-score", dest="score", action="store_false")
    p.add_argument("--write-state", action="store_true")
    p.add_argument("--max-concurrency", type=int, default=6)
    p.add_argument("--track", action="store_true", help="create Issues/Project items (needs GH_PROJECT_TOKEN)")
    p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), help="owner/name of the data repo")
    p.add_argument("--track-only", metavar="SCORED_JSONL", help="skip the pipeline; only track a previous scored.jsonl")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    data = Path(a.data_dir)
    cfg = yaml.safe_load((data / "config" / "search.yml").read_text(encoding="utf-8"))
    token = os.environ.get("GH_PROJECT_TOKEN", "")
    if (a.track or a.track_only) and not (token and a.repo):
        p.error("--track needs GH_PROJECT_TOKEN and --repo (or GITHUB_REPOSITORY)")
    if a.track_only:
        scored = [ScoredJob(**json.loads(l)) for l in Path(a.track_only).read_text(encoding="utf-8").splitlines() if l]
        res = track({"config": cfg, "scored": scored, "stats": {"track_only": True},
                     "options": {"track": True, "github_token": token, "repo": a.repo}})
        print(res)
        return 0
    raw = []
    if a.input:
        a.skip = ["jobspy", "boards"]
        raw = [Job(**json.loads(l)) for l in Path(a.input).read_text(encoding="utf-8").splitlines() if l.strip()]
    state = {"config": cfg, "ctx": Context.load(data), "raw": raw, "scored": [], "stats": {},
             "options": {"skip": a.skip, "limit": a.limit, "score": a.score, "write_state": a.write_state,
                         "track": a.track, "github_token": token, "repo": a.repo}}
    result = build().invoke(state, {"max_concurrency": a.max_concurrency})

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "jobs.jsonl").write_text("".join(j.model_dump_json() + "\n" for j in result["jobs"]), encoding="utf-8")
    (out / "scored.jsonl").write_text("".join(i.model_dump_json() + "\n" for i in result["scored"]), encoding="utf-8")
    (out / "report.md").write_text(report(result["scored"], result["stats"]), encoding="utf-8")
    print(f"{result['stats']} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
