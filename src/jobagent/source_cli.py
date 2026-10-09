"""Phase 1 entrypoint: run the LangGraph pipeline (sources → prefilter/dedupe) and write JSONL.

    python -m jobagent.source_cli --config config/search.yml --out out/jobs.jsonl [--skip jobspy]
"""
import argparse
import logging
import sys
from pathlib import Path

import yaml

from jobagent.graph import build


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--skip", nargs="*", default=[], help="sources to skip: jobspy boards")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))

    result = build().invoke({"config": cfg, "skip": a.skip, "raw": []})

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(j.model_dump_json() + "\n" for j in result["jobs"]), encoding="utf-8")
    print(f"{result['stats']} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
