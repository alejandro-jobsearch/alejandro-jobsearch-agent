"""GitHub tracker: one Issue per selected posting, added to an org Project v2 with its fields set,
plus a daily digest comment on a pinned "Digest" issue.

Two tokens: `project_token` (PAT with repo + project; Actions' GITHUB_TOKEN cannot write org Projects)
and `issues_token` (Actions' GITHUB_TOKEN). Issues and digest comments are posted by the bot, because
GitHub never notifies you about your own activity — posted with your PAT, the digest email never arrives.
"""
import json
import logging
import re
from datetime import date

import httpx

from jobagent.models import ScoredJob, _norm

log = logging.getLogger(__name__)
API = "https://api.github.com"


class Tracker:
    def __init__(self, project_token: str, repo: str, org: str, project_number: int, issues_token: str | None = None):
        self.repo, self.org = repo, org
        mk = lambda tok: httpx.Client(base_url=API, timeout=30, headers={
            "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
        self.http = mk(project_token)                                  # GraphQL / Project v2
        self.issues = mk(issues_token) if issues_token else self.http  # REST issues + comments
        self.project_id, self.fields = self._project(project_number)

    # --- GraphQL helpers -------------------------------------------------------------------------
    def _gql(self, query: str, **variables) -> dict:
        r = self.http.post("/graphql", json={"query": query, "variables": variables})
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise RuntimeError(body["errors"])
        return body["data"]

    def _project(self, number: int) -> tuple[str, dict]:
        d = self._gql("""query($org:String!,$n:Int!){ organization(login:$org){ projectV2(number:$n){ id
            fields(first:50){ nodes{ ... on ProjectV2FieldCommon{ id name dataType }
                                     ... on ProjectV2SingleSelectField{ options{ id name } } } } } } }""",
                      org=self.org, n=number)["organization"]["projectV2"]
        return d["id"], {f["name"]: f for f in d["fields"]["nodes"] if f}

    def _set(self, item_id: str, field: str, value) -> None:
        f = self.fields.get(field)
        if not f or value in (None, ""):
            return
        if f["dataType"] == "SINGLE_SELECT":
            opt = next((o["id"] for o in f["options"] if o["name"] == value), None)
            if not opt:
                return
            v = {"singleSelectOptionId": opt}
        elif f["dataType"] == "NUMBER":
            v = {"number": float(value)}
        elif f["dataType"] == "DATE":
            v = {"date": str(value)}
        else:
            v = {"text": str(value)[:250]}
        self._gql("""mutation($p:ID!,$i:ID!,$f:ID!,$v:ProjectV2FieldValue!){
            updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,fieldId:$f,value:$v}){ projectV2Item{ id } } }""",
                  p=self.project_id, i=item_id, f=f["id"], v=v)

    # --- issues ----------------------------------------------------------------------------------
    @staticmethod
    def labels(s: ScoredJob, watch: bool) -> list[str]:
        llm = s.llm or {}
        out = [f"band:{s.band}"] if s.band and s.band != "<60" else []
        if llm.get("lane") in ("A", "B", "C"):
            out.append(f"lane:{llm['lane']}")
        if llm.get("lang"):
            out.append(f"lang:{llm['lang']}")
        if llm.get("english_work") == "yes":
            out.append("english-work")
        out.append(f"src:{s.job.source}")
        if watch:
            out.append("watch")
        return out

    @staticmethod
    def body(s: ScoredJob) -> str:
        j, llm = s.job, s.llm or {}
        dims = llm.get("dims", {})
        rows = "\n".join(f"| {k} | {v} |" for k, v in dims.items())
        gaps = "\n".join(f"- {g}" for g in llm.get("gaps", [])) or "- (ninguna)"
        missing = ", ".join(s.ats_missing) or "(ninguna)"
        hist = "\n".join(f"- {h}" for h in s.company_history)
        sal = f"{j.salary_min:,.0f}–{j.salary_max:,.0f} {j.currency}" if j.salary_min else "no publicado"
        return f"""**[{j.title}]({j.url})** — {j.company} · {j.location or 's/ubicación'} · fuente: {j.source} · publicada: {j.date_posted or '?'} · salario: {sal}

**Score {s.score}** ({s.band}) · **ATS antes {s.ats_before}%** · lane {llm.get('lane', '–')} · inglés de trabajo: {llm.get('english_work', '–')} · nivel real: {llm.get('seniority_real', '–')}

> {llm.get('summary', '')}

| Dimensión | 0–100 |
|---|---|
{rows}
{"| ⚠️ requiere liderazgo operator-side | tope aplicado |" if llm.get("requires_operator_leadership") else ""}

**Brechas**
{gaps}

**Keywords ATS que faltan en el CV base:** {missing}
{f"{chr(10)}**Ya postulaste en esta empresa:**{chr(10)}{hist}" if hist else ""}

<details><summary>Descripción original</summary>

{j.description[:6000]}
</details>

<!-- job-key: {j.key} -->
<!-- jobagent-meta: {Tracker.meta(s)} -->
"""

    @staticmethod
    def meta(s: ScoredJob) -> str:
        """Machine-readable payload for the local /postular step (keywords drive the ATS-after score)."""
        llm = s.llm or {}
        return json.dumps({"url": s.job.url, "company": s.job.company, "title": s.job.title, "lang": llm.get("lang"),
                           "score": s.score, "ats_before": s.ats_before, "keywords": llm.get("keywords", []),
                           "gaps": llm.get("gaps", [])}, ensure_ascii=False).replace("--", "—")

    def create(self, s: ScoredJob, watch: bool = False) -> str:
        r = self.issues.post(f"/repos/{self.repo}/issues", json={
            "title": f"[{s.job.company}] {s.job.title}"[:250], "body": self.body(s), "labels": self.labels(s, watch)})
        r.raise_for_status()
        issue = r.json()
        item = self._gql("""mutation($p:ID!,$c:ID!){ addProjectV2ItemById(input:{projectId:$p,contentId:$c}){ item{ id } } }""",
                         p=self.project_id, c=issue["node_id"])["addProjectV2ItemById"]["item"]["id"]
        llm = s.llm or {}
        for field, value in [("Status", "Nueva"), ("Score", s.score), ("ATS antes", s.ats_before),
                             ("Empresa", s.job.company), ("Lane", llm.get("lane")),
                             ("Fecha publicacion", s.job.date_posted)]:
            self._set(item, field, value)
        return issue["html_url"]

    # --- used by the local /postular step --------------------------------------------------------
    def issue(self, number: int) -> dict:
        r = self.issues.get(f"/repos/{self.repo}/issues/{number}")
        r.raise_for_status()
        return r.json()

    def item_for_issue(self, number: int) -> str | None:
        d = self._gql("""query($o:String!,$r:String!,$n:Int!){ repository(owner:$o,name:$r){ issue(number:$n){
                projectItems(first:10){ nodes{ id project{ id } } } } } }""",
                      o=self.repo.split("/")[0], r=self.repo.split("/")[1], n=number)
        for node in d["repository"]["issue"]["projectItems"]["nodes"]:
            if node["project"]["id"] == self.project_id:
                return node["id"]
        return None

    def set_fields(self, number: int, **values) -> None:
        item = self.item_for_issue(number)
        if item:
            for field, value in values.items():
                self._set(item, field.replace("_", " "), value)

    def comment(self, number: int, body: str) -> None:
        self.issues.post(f"/repos/{self.repo}/issues/{number}/comments", json={"body": body}).raise_for_status()

    def digest(self, lines: list[str], stats: dict) -> None:
        r = self.issues.get(f"/repos/{self.repo}/issues", params={"labels": "digest", "state": "open", "per_page": 1})
        r.raise_for_status()
        if r.json():
            number = r.json()[0]["number"]
        else:
            r = self.issues.post(f"/repos/{self.repo}/issues", json={
                "title": "📬 Digest diario", "labels": ["digest"],
                "body": "Un comentario por corrida con las vacantes nuevas. Suscríbete a este issue para recibirlo por correo."})
            r.raise_for_status()
            number = r.json()["number"]
        bands = ", ".join(f"{k}: {v}" for k, v in (stats.get("bands") or {}).items()) or "–"
        foot = (f"Avisos revisados: {stats.get('raw', '–')} · tras filtro: {stats.get('prefiltered', '–')} · "
                f"nuevas puntuadas: {stats.get('new', '–')} · bandas: {bands}")
        text = f"### {date.today().isoformat()}\n\n" + ("\n".join(lines) if lines else "Sin vacantes nuevas sobre el umbral.") + \
               f"\n\n<sub>{foot}</sub>"
        self.issues.post(f"/repos/{self.repo}/issues/{number}/comments", json={"body": text}).raise_for_status()


def select(scored: list[ScoredJob], cfg: dict) -> list[tuple[ScoredJob, bool]]:
    """(item, is_watch) for postings that deserve an issue: score ≥ min_score, or a watched company ≥ watch_min."""
    names = [re.escape(_norm(w)) for w in cfg.get("watch_companies", [])]
    watch_re = re.compile(rf"\b({'|'.join(names)})\b") if names else None
    out = []
    for s in scored:
        if s.applied_match or s.score is None:
            continue
        if s.score >= cfg["min_score"]:
            out.append((s, False))
        elif s.score >= cfg["watch_min"] and watch_re and watch_re.search(_norm(s.job.company)):
            out.append((s, True))
    return sorted(out, key=lambda t: -t[0].score)
