"""
llm_client.py
-------------
Shared LLM client factory for the Protiviti Operational Audit Assistant.

Provider priority:
  1. Azure OpenAI  — used when AZURE_ENDPOINT is set in the environment.
     Supports an optional SUBSCRIPTION_KEY header for corporate API gateways.
  2. Standard OpenAI — default, used when AZURE_ENDPOINT is not set.
     Works out-of-the-box in the sandbox using the pre-configured OPENAI_API_KEY.

Usage:
    from core.llm_client import get_llm_client, get_llm_model, build_completion_kwargs

    client = get_llm_client()
    model  = get_llm_model()
    resp   = client.chat.completions.create(
        model=model,
        messages=[...],
        **build_completion_kwargs(max_tokens=1000, temperature=0.2),
    )
"""

import logging
import os

from openai import AzureOpenAI, OpenAI

logger = logging.getLogger(__name__)

_client: OpenAI | None = None
_model: str | None = None

_DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
_DEFAULT_AZURE_MODEL  = "gpt-4o"


def get_llm_client() -> OpenAI:
    """Return the appropriate LLM client (Azure or standard OpenAI). Cached for process lifetime."""
    global _client, _model
    if _client is not None:
        return _client

    azure_endpoint = os.environ.get("AZURE_ENDPOINT", "").strip()

    if azure_endpoint:
        deployment  = os.environ.get("AZURE_OPENAI_DEPLOYMENT", _DEFAULT_AZURE_MODEL)
        api_key     = os.environ.get("API_KEY", "")
        api_version = os.environ.get("API_VERSION", "2024-02-01")
        sub_key     = os.environ.get("SUBSCRIPTION_KEY", "").strip()

        default_headers: dict = {}
        if sub_key:
            default_headers[sub_key] = api_key

        _client = AzureOpenAI(
            azure_endpoint=azure_endpoint,
            api_key=api_key,
            api_version=api_version,
            default_headers=default_headers if default_headers else None,
        )
        _model = deployment
        logger.info("LLM client: Azure OpenAI | endpoint=%s | deployment=%s", azure_endpoint, deployment)
    else:
        _client = OpenAI()
        _model = os.environ.get("OPENAI_MODEL", _DEFAULT_OPENAI_MODEL)
        logger.info("LLM client: Standard OpenAI | model=%s", _model)

    return _client


def get_llm_model() -> str:
    """Return the model/deployment name to use for completions."""
    if _model is None:
        get_llm_client()
    return _model  # type: ignore[return-value]


def is_o_series() -> bool:
    """Return True if the configured model is an o-series model (o1, o3, etc.)."""
    if _model is None:
        get_llm_client()
    forced = os.environ.get("IS_O_SERIES", "").lower() == "true"
    if forced:
        return True
    name = (_model or "").lower()
    return (
        any(tok in name for tok in ("-o1", "-o3", "/o1", "/o3"))
        or name.startswith("o1")
        or name.startswith("o3")
    )


def build_completion_kwargs(max_tokens: int, temperature: float | None = None) -> dict:
    """Return correct kwargs for chat.completions.create, handling o-series differences."""
    if is_o_series():
        return {"max_completion_tokens": max_tokens}
    kwargs: dict = {"max_tokens": max_tokens}
    if temperature is not None:
        kwargs["temperature"] = temperature
    return kwargs


def build_response_format_kwargs(format_type: str = "json_object") -> dict:
    """Return response_format kwarg only when safe (not for o-series)."""
    if is_o_series():
        return {}
    return {"response_format": {"type": format_type}}


def reset_client() -> None:
    """Reset the cached client (useful for testing)."""
    global _client, _model
    _client = None
    _model = None
