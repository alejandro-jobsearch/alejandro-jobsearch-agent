"""OpenAI-compatible LLM client (developed against GLM on Huawei Cloud ModelArts MaaS)."""
import os

from openai import OpenAI


def client() -> OpenAI:
    return OpenAI(base_url=os.environ["MAAS_BASE_URL"], api_key=os.environ["MAAS_API_KEY"])


def model() -> str:
    return os.environ.get("MAAS_MODEL", "glm-5.3")
