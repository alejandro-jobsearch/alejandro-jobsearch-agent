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
    bumeran_last_id: int = 0                # highest Bumeran posting id already looked at (state/bumeran.json)
    exa_last: str | None = None             # date of the last web search (state/exa.json)
    aliases: dict[str, list[str]] = field(default_factory=dict)   # applied.yml company_aliases: name -> other names

    def canon(self, company: str) -> str:
        """Normalized company name, with known aliases mapped to one name: copies on aggregators or in other
        languages ("윌리스타워스왓슨(Willis Towers Watson)", "Ph Data in LATAM") must still meet the history."""
        c = _norm(company)
        for name, others in self.aliases.items():
            for alias in [name, *others]:
                if re.search(rf"\b{re.escape(_norm(alias))}\b", c):
                    return _norm(name)
        return c

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
        bumeran_file = d / "state" / "bumeran.json"
        exa_file = d / "state" / "exa.json"
        history = yaml.safe_load((d / "config" / "applied.yml").read_text(encoding="utf-8"))
        return cls(
            data_dir=d,
            criteria=(d / "config" / "criteria.md").read_text(encoding="utf-8"),
            cv={lang: (d / "cv" / f"cv-{lang}.md").read_text(encoding="utf-8") for lang in ("en", "es")},
            applied=history["applied"],
            aliases=history.get("company_aliases") or {},
            seen=seen,
            bumeran_last_id=json.loads(bumeran_file.read_text(encoding="utf-8"))["last_id"]
            if bumeran_file.exists() else 0,
            exa_last=json.loads(exa_file.read_text(encoding="utf-8"))["last"] if exa_file.exists() else None,
        )

    def is_seen(self, j: Job) -> bool:
        return j.key in self.seen or j.url in self.seen

    def employer_in(self, text: str) -> str | None:
        """A company from the application history named near the top of a page: an aggregator copy (Toka,
        Remotery, brenxor) is filed under the aggregator's name, so it never met the history (phData #106, its
        fourth copy). Names under 4 letters ("AWS") are skipped: partners are named in every consulting ad."""
        head = _norm(text[:800])
        names = {a["company"] for a in self.applied} | {n for k, v in self.aliases.items() for n in [k, *v]}
        for n in sorted(names, key=len, reverse=True):
            if len(_norm(n)) >= 4 and re.search(rf"\b{re.escape(_norm(n))}\b", head):
                return n
        return None

    def applied_match(self, j: Job) -> tuple[str | None, list[str]]:
        """Fuzzy match against the application history.

        Returns (same role already applied, other roles applied at the same company). A same-titled application
        that failed does not block: the posting is new (seen postings never get here), so the search may have
        been relaunched. It goes to the history, for the reader to judge."""
        company, title = self.canon(j.company), _norm(j.title)
        same, others = None, []
        for a in self.applied:
            if fuzz.token_set_ratio(company, self.canon(a["company"])) < 85:
                continue
            label = f'{a["company"]} — {a["role"]}' + (f' ({a["note"]})' if a.get("note") else "")
            if a["role"] and fuzz.token_sort_ratio(title, _norm(a["role"])) >= 80:
                if FAILED.search(a.get("note") or ""):
                    others.append(f"posible relanzamiento: {label}")
                else:
                    same = same or label
            else:
                others.append(label)
        return same, others

    def paused(self, j: Job, after: int, referrals: list[str]) -> str | None:
        """Why the company is paused: `after` or more past applications there closed with no answer or a
        rejection, and no referral listed for it. None when it is open. Company names must match closely
        (sort ratio, not set ratio): "Acme Capital" is not the "Acme" group."""
        company = self.canon(j.company)
        if any(fuzz.token_sort_ratio(company, self.canon(r)) >= 90 for r in referrals):
            return None
        failed = [a["role"] for a in self.applied
                  if fuzz.token_sort_ratio(company, self.canon(a["company"])) >= 90 and FAILED.search(a.get("note") or "")]
        return f"{len(failed)} postulaciones sin éxito ({'; '.join(failed)}): solo con referido"             if len(failed) >= after else None

    def remember(self, jobs: list[Job], today: str) -> None:
        f = self.data_dir / "state" / "seen.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            for j in jobs:
                fh.write(json.dumps({"key": j.key, "url": j.url, "seen": today}, ensure_ascii=False) + "\n")

    def found_boards(self) -> list[dict]:
        """Boards added by `add_boards` (state/boards.json), read by `source_boards` next to the configured ones."""
        f = self.data_dir / "state" / "boards.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []

    def add_boards(self, boards: list[dict], today: str) -> list[dict]:
        """Keep boards not yet known (same provider + slug). Returns the new ones."""
        f = self.data_dir / "state" / "boards.json"
        known = self.found_boards()
        keys = {(b["provider"], b["slug"].lower()) for b in known}
        new = []
        for b in boards:
            if (b["provider"], b["slug"].lower()) not in keys:
                keys.add((b["provider"], b["slug"].lower()))
                new.append({**b, "found": today})
        if new:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(known + new, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return new

    def save_exa(self, today: str) -> None:
        f = self.data_dir / "state" / "exa.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"last": today}) + "\n", encoding="utf-8")

    def save_bumeran(self, last_id: int, today: str) -> None:
        f = self.data_dir / "state" / "bumeran.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"last_id": last_id, "updated": today}) + "\n", encoding="utf-8")
