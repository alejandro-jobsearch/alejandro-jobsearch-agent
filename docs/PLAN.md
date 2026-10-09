# Plan — Agente de búsqueda de empleo (LangGraph + GitHub Actions + GLM)

Fecha: 2026-10-08 · Owner: Alejandro More · Org: [alejandro-jobsearch](https://github.com/alejandro-jobsearch)

## 1. Objetivo

Automatizar el ciclo **buscar → filtrar → puntuar (ATS antes) → decidir → adaptar CV → puntuar (ATS después) → preparar brechas → trackear**, con un humano aprobando antes de postular. Nada se envía automáticamente.

## 2. Arquitectura: dos repositorios

| Repo | Visibilidad | Contenido |
|---|---|---|
| `alejandro-jobsearch-agent` (este) | **Público** | Motor: código Python + LangGraph, workflow reutilizable, configs de ejemplo, tests. Sin datos personales. Pieza de portafolio. |
| `alejandro-jobsearch-data` | **Privado** | `config/criteria.yml` (piso salarial, lanes, exclusiones), `config/applied.yml` (historial), texto de CV base EN/ES, Issues = vacantes, GitHub Project = kanban. **Aquí van los secrets y aquí corre el cron.** |

**Por qué así:** en un repo público los logs de Actions y los Issues son públicos. Tu empleador o un reclutador podría ver a qué postulas y tu piso salarial. El cron corre en el repo privado y llama al workflow reutilizable del público (`workflow_call`). Un cron diario de unos 5 minutos son ~150 min/mes, dentro de los 2,000 min gratis de un repo privado.

```
alejandro-jobsearch-data (privado)              alejandro-jobsearch-agent (público)
  .github/workflows/daily.yml  ──uses──▶  .github/workflows/pipeline.yml (workflow_call)
  secrets: MAAS_API_KEY                         src/jobagent/ (LangGraph)
  config/*.yml  ◀──lee── ┐                      config/examples/*.yml
  Issues + Project  ◀──escribe── pipeline
```

## 3. Stack

- Python 3.12, `uv` para dependencias
- `langgraph` (grafo + checkpoint + `interrupt` para la aprobación humana)
- `python-jobspy` (LinkedIn/Indeed/Glassdoor/Google sin login) + `httpx` para las APIs públicas de Greenhouse, Lever y Ashby
- `openai` SDK apuntando a **Huawei ModelArts MaaS** (compatible con OpenAI)
  - `MAAS_BASE_URL=https://api-ap-southeast-1.modelarts-maas.com/openai/v1`
  - `MAAS_MODEL=glm-5.3`
- `pydantic` (esquemas `Job`, `Score`), `rapidfuzz` + SimHash (dedupe), `python-docx` (leer CV)
- GitHub: Issues + Projects v2 vía `gh api` / GraphQL

## 4. El grafo (LangGraph)

```
source ─▶ normalize ─▶ dedupe ─▶ hard_filter ─▶ ats_before ─▶ llm_score ─▶ route
                                                                             │
                                          score ≥ 75 ─▶ create_issue ─▶ digest
                                          60–74 + empresa objetivo ─▶ create_issue (label: watch)
                                          < 60 ─▶ discard_log
---------------------------------- local (a pedido) ----------------------------------
issue aprobado ─▶ tailor_cv ─▶ ats_after ─▶ gap_analysis ─▶ [gap_deck] ─▶ update_issue
```

- **Nube (Actions):** de `source` a `digest`. Sin escribir archivos docx.
- **Local (Claude Code):** de `tailor_cv` en adelante, porque el chequeo de 2 páginas usa Word COM (Windows). Se dispara con un comando, por ejemplo `/postular 42`.

## 5. Scoring

1. **ATS antes (determinístico, sin LLM):** se extraen keywords de la vacante (skills, certificaciones, herramientas, seniority) y se mide su cobertura en el texto del CV base, en el idioma de la vacante. Resultado: `% cobertura` + keywords faltantes. Idea tomada de `career-ops/keyword-match.mjs` (MIT).
2. **Match LLM (glm-5.3):** la rúbrica de `01-Criterios-Match.md` §9 (seniority 25, dominio 25, industria 20, operator/vendor 15, geo 10, compensación 5) + la regla anti-falso-positivo §8 como instrucciones de sistema. Salida en JSON validado con pydantic: score por dimensión, banda, brechas, inglés sí/no, justificación de 2 líneas.
3. **ATS después:** el mismo cálculo del punto 1 sobre el CV adaptado. El Issue muestra el delta (por ejemplo 58% → 84%).

Regla dura: **las keywords se reformulan, nunca se inventan.** Si una brecha es real, va al deck de preparación, no al CV.

## 6. Tracking

- **1 Issue por vacante** en el repo privado. Título: `[Empresa] Rol`. Cuerpo: link, fuente, fecha, score por dimensión, ATS antes/después, brechas.
- **Labels:** `lane:A|B|C`, `band:90+|75-89|60-74`, `lang:en|es`, `src:linkedin|greenhouse|…`, `english-work`
- **Project v2 (kanban):** Nueva → Evaluada → CV listo → Postulada → Entrevista → Oferta / Cerrada
- **Dedupe persistente:** `state/seen.jsonl` en el repo privado (hash de URL + empresa/rol normalizado + SimHash del texto de la vacante).
- **Digest diario:** un comentario en un Issue fijado "Digest" con las nuevas del día, ordenadas por score.

## 7. Fases

| Fase | Entregable | Criterio de "hecho" |
|---|---|---|
| **0. Setup** | 2 repos, secret `MAAS_API_KEY`, variables MAAS, labels, Project | `gh workflow run` corre un "hello LLM" contra glm-5.3 |
| **1. Sourcing** | `sources/jobspy.py`, `sources/greenhouse.py`, `lever.py`, `ashby.py` → `Job` normalizado | Corrida local devuelve ≥ 50 vacantes con los términos de los lanes A/B/C |
| **2. Filtro + dedupe** | Exclusiones §7, historial §11, antigüedad ≤ 60 días, `seen.jsonl` | Re-ejecutar no crea duplicados; las vacantes ya postuladas salen marcadas |
| **3. Scoring** | `ats.py` (antes) + `llm_score.py` (glm-5.3, JSON) | 10 vacantes históricas puntuadas con un error de ±10 pts frente a tu juicio (calibración) |
| **4. Tracker + cron** | `tracker.py` (Issues + Project), `daily.yml` en el repo privado | Una semana de corridas diarias sin intervención |
| **5. Local / postular** | Skill de Claude Code `/postular <issue>`: CV docx adaptado + ATS después + deck (reusa `prep_gap_deck.py`) | 1 postulación real cerrada de punta a punta con el Issue actualizado |
| **6. Portafolio** | README con diagrama, demo con datos ficticios, tests, badge de CI | Repo fijado en tu perfil |
| **7. Métricas** (opcional) | Embudo por lane/fuente/score → tasa de respuesta | Reporte mensual automático |

## 8. Riesgos

| Riesgo | Mitigación |
|---|---|
| LinkedIn bloquea las IPs de Actions (429) | Bajo volumen (≤ 3 búsquedas, `hours_old=24`); si falla, la fuente LinkedIn corre en local y sube el resultado |
| Los términos de servicio de LinkedIn/Indeed prohíben el scraping | Solo lectura de endpoints públicos de invitado, uso personal y bajo volumen; las APIs de ATS son públicas y no tienen ese problema |
| Datos personales filtrados en el repo público | `.gitignore` estricto; config real solo en el repo privado; los ejemplos usan datos ficticios |
| Cuota o latencia de MaaS | Prefiltro determinístico antes del LLM y caché por hash de la vacante |
| Sobreestimar el match | Regla §8 en el prompt + calibración contra casos conocidos (ManpowerGroup 90→73) |

## 9. Reutilización de career-ops (MIT, en `reference/`, no versionado)

- `fingerprint-core.mjs` → SimHash para detectar la misma vacante re-publicada por una agencia
- `keyword-match.mjs` → enfoque de cobertura ATS
- `templates/ats-rules.yml`, `templates/portals.example.yml` → reglas ATS y lista de empresas con Greenhouse/Lever/Ashby
- `modes/` → ideas de prompts de evaluación

No se ejecuta su código. Solo se usa como referencia de diseño.

## 10. Bitácora

**2026-10-08 — Fase 0 (parcial)**
- Org `alejandro-jobsearch` con los 2 repos; local en `C:\Projects\alejandro-jobsearch\`.
- El secret `MAAS_API_KEY` está en el **environment `poc`** del repo privado. Los jobs que lo usen declaran `environment: poc`, que en el workflow reutilizable se recibe como input.
- `llm-smoke` en verde. glm-5.3 es un **modelo de razonamiento**: la completion simple tarda 1.6 s y la respuesta en JSON (`response_format=json_object`) unos 15 s. Implicancia: antes de llamar al LLM hay que aplicar el prefiltro determinístico, y el scoring en lotes debe correr en paralelo (5–8 workers).
- Labels del tracker creados en el repo privado.
- Pendiente: tablero Project v2. Necesita el scope `project` en `gh` y un PAT clásico con `project` como secret para Actions.
