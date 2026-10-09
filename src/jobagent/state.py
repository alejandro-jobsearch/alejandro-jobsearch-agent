"""Data-repo context: CVs, criteria, application history and the persistent `seen` set."""
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from rapidfuzz import fuzz

from jobagent.models import Job, _norm


@dataclass
class Context:
    data_dir: Path
    criteria: str
    cv: dict[str, str]                      # lang -> plain text
    applied: list[dict]
    seen: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, data_dir: str | Path) -> "Context":
        d = Path(data_dir)
        seen_file = d / "state" / "seen.jsonl"
        seen = set()
        if seen_file.exists():
            for line in seen_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    seen |= {r["key"], r["url"]}
        return cls(
            data_dir=d,
            criteria=(d / "config" / "criteria.md").read_text(encoding="utf-8"),
            cv={lang: (d / "cv" / f"cv-{lang}.md").read_text(encoding="utf-8") for lang in ("en", "es")},
            applied=yaml.safe_load((d / "config" / "applied.yml").read_text(encoding="utf-8"))["applied"],
            seen=seen,
        )

    def is_seen(self, j: Job) -> bool:
        return j.key in self.seen or j.url in self.seen

    def applied_match(self, j: Job) -> tuple[str | None, list[str]]:
        """Fuzzy match against the application history.

        Returns (same role already applied, other roles applied at the same company)."""
        company, title = _norm(j.company), _norm(j.title)
        same, others = None, []
        for a in self.applied:
            if fuzz.token_set_ratio(company, _norm(a["company"])) < 85:
                continue
            label = f'{a["company"]} — {a["role"]}' + (f' ({a["note"]})' if a.get("note") else "")
            if a["role"] and fuzz.token_sort_ratio(title, _norm(a["role"])) >= 80:
                same = same or label
            else:
                others.append(label)
        return same, others

    def remember(self, jobs: list[Job], today: str) -> None:
        f = self.data_dir / "state" / "seen.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            for j in jobs:
                fh.write(json.dumps({"key": j.key, "url": j.url, "seen": today}, ensure_ascii=False) + "\n")
