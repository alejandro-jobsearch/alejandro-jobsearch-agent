"""Data-repo context: CVs, criteria, application history and the persistent `seen` set."""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from rapidfuzz import fuzz

from jobagent.models import Job, _norm

FAILED = re.compile(r"RECHAZAD|REJECTED|SIN RESPUESTA|NO AVANZ", re.I)


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

    def paused(self, j: Job, after: int, referrals: list[str]) -> str | None:
        """Why the company is paused: `after` or more past applications there closed with no answer or a
        rejection, and no referral listed for it. None when it is open. Company names must match closely
        (sort ratio, not set ratio): "Acme Capital" is not the "Acme" group."""
        company = _norm(j.company)
        if any(fuzz.token_sort_ratio(company, _norm(r)) >= 90 for r in referrals):
            return None
        failed = [a["role"] for a in self.applied
                  if fuzz.token_sort_ratio(company, _norm(a["company"])) >= 90 and FAILED.search(a.get("note") or "")]
        return f"{len(failed)} postulaciones sin éxito ({'; '.join(failed)}): solo con referido"             if len(failed) >= after else None

    def remember(self, jobs: list[Job], today: str) -> None:
        f = self.data_dir / "state" / "seen.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            for j in jobs:
                fh.write(json.dumps({"key": j.key, "url": j.url, "seen": today}, ensure_ascii=False) + "\n")
