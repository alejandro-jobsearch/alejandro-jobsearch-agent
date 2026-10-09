# alejandro-jobsearch-agent

Agentic job-search pipeline built with **LangGraph**: sources openings from public job boards and ATS APIs, filters and scores them against a candidate profile (deterministic ATS keyword coverage + LLM rubric), and tracks them as GitHub Issues on a Project board. A human approves every application — nothing is auto-submitted.

- **Runtime:** GitHub Actions (scheduled, reusable workflow)
- **LLM:** any OpenAI-compatible endpoint (developed against GLM on Huawei Cloud ModelArts MaaS)
- **Tracking:** GitHub Issues + Projects v2

> Personal data (CVs, criteria, history) lives in a separate private repository. This repo only contains the engine and fictional example configs.

Status: 🚧 phase 0 — see [docs/PLAN.md](docs/PLAN.md).

## License

MIT
