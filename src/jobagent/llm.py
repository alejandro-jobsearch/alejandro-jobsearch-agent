"""OpenAI-compatible LLM client (developed against GLM on Huawei Cloud ModelArts MaaS).

`LLM_PROVIDER` picks the endpoint: `maas` (default) reads MAAS_BASE_URL / MAAS_API_KEY / MAAS_MODEL, and
`openrouter` reads OPENROUTER_API_KEY / OPENROUTER_MODEL (OPENROUTER_BASE_URL optional). Both key sets can stay
configured at once, so switching is a one-variable change.
"""
import os

from openai import OpenAI

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"


def provider() -> str:
    p = (os.environ.get("LLM_PROVIDER") or "maas").strip().lower()
    if p not in ("maas", "openrouter"):
        raise ValueError(f"LLM_PROVIDER must be 'maas' or 'openrouter', got {p!r}")
    return p


def client() -> OpenAI:
    if provider() == "openrouter":
        return OpenAI(base_url=os.environ.get("OPENROUTER_BASE_URL") or OPENROUTER_BASE_URL,
                      api_key=os.environ["OPENROUTER_API_KEY"])
    return OpenAI(base_url=os.environ["MAAS_BASE_URL"], api_key=os.environ["MAAS_API_KEY"])


def model() -> str:
    if provider() == "openrouter":
        return os.environ.get("OPENROUTER_MODEL") or OPENROUTER_MODEL
    return os.environ.get("MAAS_MODEL", "glm-5.3")
