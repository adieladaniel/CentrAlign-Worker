"""Build the configured LLM client."""
from __future__ import annotations

from worker.config import Settings
from worker.llm.base import LLMClient, LLMError
from worker.llm.fake import ScriptedLLM
from worker.llm.resilient import ResilientLLM


def make_llm(settings: Settings) -> LLMClient:
    """Create the provider named by ``settings.LLM_PROVIDER``, wrapped in ResilientLLM.

    The ``fake`` provider returns an empty ScriptedLLM (tests replace its script) with no throttling.
    """
    provider = settings.LLM_PROVIDER
    if provider == "gemini":
        from worker.llm.failover import FailoverLLM
        from worker.llm.gemini import GeminiClient

        if not settings.GEMINI_API_KEY:
            raise LLMError("GEMINI_API_KEY is not set (get a free key at https://aistudio.google.com/apikey)")
        key = settings.GEMINI_API_KEY
        inner: LLMClient = FailoverLLM(settings.gemini_models, lambda model: GeminiClient(key, model))
    elif provider == "ollama":
        from worker.llm.ollama import OllamaClient

        inner = OllamaClient(settings.OLLAMA_URL, settings.OLLAMA_MODEL)
    elif provider == "fake":
        return ResilientLLM(ScriptedLLM([]), min_interval_s=0.0)
    else:
        raise LLMError(f"Unknown LLM_PROVIDER '{provider}'")
    return ResilientLLM(
        inner, min_interval_s=settings.LLM_MIN_INTERVAL_S, network_retry_s=settings.LLM_NETWORK_RETRY_S
    )
