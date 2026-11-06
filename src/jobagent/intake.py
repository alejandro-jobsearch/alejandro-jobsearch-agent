"""Evaluate a user-submitted Bumeran issue without browsing or scraping Bumeran."""
import argparse
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

import yaml

from jobagent import ats, scoring
from jobagent.models import Job, ScoredJob
from jobagent.state import Context
from jobagent.tracker import Tracker

log = logging.getLogger(__name__)
LIMA = ZoneInfo("America/Lima")


def _field(body: str, heading: str) -> str:
    headings = ("Enlace del aviso", "Empresa", "Cargo", "Fecha de publicación",
                "Ubicación y modalidad", "Salario, si figura", "Descripción completa")
    others = "|".join(re.escape(h) for h in headings if h != heading)
    pattern = rf"(?ms)^###\s+{re.escape(heading)}\s*\n(.*?)(?=^###\s+(?:{others})\s*$|\Z)"
    match = re.search(pattern, body)
    value = match.group(1).strip() if match else ""
    if value in {"_No response_", "_No response", "No response"}:
        return ""
    return value


def _published_date(value: str) -> date:
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError("La fecha debe tener formato AAAA-MM-DD o DD-MM-AAAA.")


def _salary(value: str) -> tuple[float | None, float | None, str | None]:
    if not value:
        return None, None, None
    currency = "PEN" if re.search(r"\bPEN\b|S/", value, re.I) else "USD" if re.search(r"\bUSD\b|US\$", value, re.I) else None
    raw = re.findall(r"\d[\d,.]*", value)
    if not raw:
        return None, None, currency

    def number(s: str) -> float:
        s = s.replace(" ", "")
        if "," in s and "." in s:
            s = s.replace(",", "") if s.rfind(",") < s.rfind(".") else s.replace(".", "").replace(",", ".")
        elif "," in s:
            tail = s.rsplit(",", 1)[-1]
            s = s.replace(",", ".") if len(tail) <= 2 else s.replace(",", "")
        elif s.count(".") > 1:
            s = s.replace(".", "")
        return float(s)

    amounts = [number(x) for x in raw[:2]]
    return amounts[0], amounts[-1], currency


def _job_from_issue(issue: dict) -> Job:
    body = issue.get("body") or ""
    url = _field(body, "Enlace del aviso")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"bumeran.com.pe", "www.bumeran.com.pe"} or not parsed.path.startswith("/empleos/"):
        raise ValueError("El enlace debe ser un aviso de empleo de Bumeran Perú.")

    company = _field(body, "Empresa")
    title = _field(body, "Cargo")
    description = _field(body, "Descripción completa")
    if not company or not title:
        raise ValueError("Completa Empresa y Cargo en el formulario.")
    if len(description) < 150:
        raise ValueError("Pega la descripción completa del aviso (mínimo 150 caracteres); no se evaluarán solo títulos.")

    posted = _published_date(_field(body, "Fecha de publicación"))
    location = _field(body, "Ubicación y modalidad") or "Perú"
    salary_min, salary_max, currency = _salary(_field(body, "Salario, si figura"))
    remote = True if re.search(r"remoto|remote", location, re.I) else None
    return Job(source="bumeran", company=company, title=title, location=location, url=url,
               description=description, date_posted=posted, remote=remote,
               salary_min=salary_min, salary_max=salary_max, currency=currency)


def evaluate_issue(issue_number: int, data_dir: str, repo: str) -> str:
    data = Path(data_dir)
    cfg = yaml.safe_load((data / "config" / "search.yml").read_text(encoding="utf-8"))
    tracker_cfg = cfg["tracker"]
    tracker = Tracker(project_token=os.environ["GH_PROJECT_TOKEN"], repo=repo,
                      org=tracker_cfg["org"], project_number=tracker_cfg["project_number"],
                      issues_token=os.environ.get("GH_ISSUES_TOKEN"))
    issue = tracker.issue(issue_number)
    try:
        job = _job_from_issue(issue)
    except ValueError as exc:
        tracker.comment(issue_number, f"No se pudo evaluar este intake: {exc}\n\nEdita el issue con los datos indicados para reintentar.")
        raise
    ctx = Context.load(data)

    same, company_history = ctx.applied_match(job)
    llm = scoring.score(job, ctx.criteria)
    age_days = (datetime.now(LIMA).date() - job.date_posted).days
    if age_days < 0:
        raise ValueError("La fecha de publicación no puede estar en el futuro.")
    if age_days > 60:
        llm.hard_exclusion = True
        llm.exclusion_reason = f"Aviso publicado hace {age_days} días; criterio de antigüedad: máximo 60 días"
        llm.gaps = [llm.exclusion_reason, *llm.gaps][:6]
        llm.summary = f"Se descarta por antigüedad ({age_days} días desde su publicación). {llm.summary}"

    total = scoring.total(llm, cfg["scoring"])
    keywords = [k.model_dump() for k in llm.keywords]
    coverage, missing = ats.coverage(keywords, ctx.cv[llm.lang])
    result = ScoredJob(job=job, applied_match=same, company_history=company_history, score=total,
                       band=scoring.band(total), ats_before=coverage, ats_missing=missing,
                       llm=llm.model_dump())

    status = "Postulada" if same else "Evaluada"
    intake_url = issue["html_url"]
    existing = next((x for x in tracker.items() if x["url"] == job.url and x["number"] != issue_number), None)
    if existing:
        status = "Postulada" if same else (existing["status"] or "Evaluada")
        tracker.update_existing(existing["number"], result, status=status)
        tracker.comment(issue_number, f"Ingreso consolidado en el issue #{existing['number']}: mismo enlace de Bumeran.")
        tracker.issues.patch(f"/repos/{repo}/issues/{issue_number}",
                             json={"state": "closed", "state_reason": "not_planned"}).raise_for_status()
        return existing["url"]

    return tracker.update_existing(issue_number, result, status=status) or intake_url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    url = evaluate_issue(args.issue, args.data_dir, args.repo)
    print(f"Bumeran evaluado: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
