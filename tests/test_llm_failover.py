"""Model failover, quota classification, signature handling on model switches, and thought summaries (no network)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.genai import errors as genai_errors
from google.genai import types

from worker.agent.context import transcript_messages
from worker.agent.runner import THOUGHT_LIMIT, AgentRunner
from worker.agent.state import Step
from worker.config import Settings
from worker.llm.base import (
    LLMResponse,
    Message,
    ModelUnavailableError,
    QuotaExhaustedError,
    RateLimitError,
    ToolCall,
    ToolSpec,
    TransientLLMError,
    Usage,
)
from worker.llm.failover import ALL_EXHAUSTED_MESSAGE, FailoverLLM, ModelHealth, next_quota_reset
from worker.llm.gemini import DUMMY_THOUGHT_SIGNATURE, GeminiClient, map_error, parse_response, to_gemini_contents
from worker.llm.resilient import ResilientLLM

SPEC = ToolSpec("ping", "ping", {"type": "object", "properties": {"n": {"type": "integer"}}})


def ok(text="", calls=(), thought="") -> LLMResponse:
    return LLMResponse(text=text, tool_calls=list(calls), usage=Usage(10, 5), thought=thought)


# ---------------------------------------------------------------- error classification


def _429(quota_id="", delay="13s", message="quota"):
    details = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]
    if quota_id:
        details.append({"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota_id, "quotaValue": "20"}]})
    return genai_errors.ClientError(429, {"error": {"code": 429, "message": message, "status": "RESOURCE_EXHAUSTED", "details": details}})


def test_daily_quota_id_maps_to_quota_exhausted_with_model():
    mapped = map_error(_429("GenerateRequestsPerDayPerProjectPerModel-FreeTier", delay="5s"), "gemini-3.8-flash")
    assert isinstance(mapped, QuotaExhaustedError) and mapped.model == "gemini-3.8-flash"


def test_long_retry_delay_means_daily_quota_but_short_per_minute_is_a_rate_limit():
    assert isinstance(map_error(_429(delay="3600s"), "m"), QuotaExhaustedError)
    minute = map_error(_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", delay="13s"), "m")
    assert isinstance(minute, RateLimitError) and not isinstance(minute, QuotaExhaustedError) and minute.retry_after == 13.0


def test_503_is_transient_unavailable_and_404_is_model_unavailable():
    err503 = map_error(genai_errors.ServerError(503, {"error": {"message": "high demand", "status": "UNAVAILABLE"}}), "m")
    assert isinstance(err503, TransientLLMError) and err503.unavailable
    err500 = map_error(genai_errors.ServerError(500, {"error": {"message": "boom"}}), "m")
    assert isinstance(err500, TransientLLMError) and not err500.unavailable
    err404 = map_error(genai_errors.ClientError(404, {"error": {"message": "not found", "status": "NOT_FOUND"}}), "gemini-9")
    assert isinstance(err404, ModelUnavailableError) and err404.model == "gemini-9"


# ---------------------------------------------------------------- FailoverLLM


class StubModel:
    """Per-model scripted client: each queue item is an exception to raise or a response to return."""

    name = "stub"

    def __init__(self, model, items):
        self.model, self.items, self.calls = model, list(items), 0

    async def complete(self, messages, tools=None, **kwargs):
        self.calls += 1
        item = self.items.pop(0) if self.items else ok(f"from {self.model}")
        if isinstance(item, Exception):
            raise item
        return item


def make_failover(plan, **kwargs):
    clients = {m: StubModel(m, items) for m, items in plan.items()}
    sleeps: list[float] = []

    async def sleep(s):
        sleeps.append(s)

    kwargs.setdefault("health", ModelHealth())
    llm = FailoverLLM(list(plan), lambda m: clients[m], sleep=sleep, **kwargs)
    return llm, clients, sleeps


async def test_daily_quota_fails_over_to_next_model_and_is_remembered():
    llm, clients, _ = make_failover({"a": [QuotaExhaustedError("20/day", model="a")], "b": [], "c": []})
    first = await llm.complete([Message("user", "hi")])
    assert first.text == "from b" and llm.active_model == "b"
    assert clients["a"].calls == 1  # no retry on the exhausted model
    await llm.complete([Message("user", "again")])
    assert clients["a"].calls == 1 and clients["b"].calls == 2  # exhausted model is not even tried again
    # a new client in the same process (next dashboard run) shares the memory
    second = FailoverLLM(["a", "b", "c"], lambda m: clients[m], health=llm.health)
    assert (await second.complete([Message("user", "x")])).text == "from b" and clients["a"].calls == 1


async def test_exhausted_models_are_retried_the_next_day():
    now = [1_800_000_000.0]
    health = ModelHealth(clock=lambda: now[0])
    llm, clients, _ = make_failover({"a": [QuotaExhaustedError("x", model="a")], "b": []}, health=health)
    await llm.complete([Message("user", "hi")])
    assert health.reason("a") == "quota"
    now[0] += 3 * 3600
    assert health.reason("a") == "quota"  # still the same quota day
    now[0] = next_quota_reset(1_800_000_000.0) + 1
    assert health.reason("a") is None
    assert (await llm.complete([Message("user", "hi")])).text == "from a"  # strongest model is back in front


async def test_model_switches_counted_and_exposed_in_stats():
    llm, _, _ = make_failover({"a": [ok("one"), QuotaExhaustedError("x", model="a")], "b": []})
    resilient = ResilientLLM(llm, min_interval_s=0.0)
    await resilient.complete([Message("user", "1")])
    assert resilient.stats["model"] == "a" and resilient.stats["model_switches"] == 0
    await resilient.complete([Message("user", "2")])
    assert resilient.stats["model"] == "b" and resilient.stats["model_switches"] == 1


async def test_per_minute_rate_limit_retries_the_same_model():
    llm, clients, _ = make_failover({"a": [RateLimitError("slow down", retry_after=2.0)], "b": []})
    waits: list[float] = []

    async def sleep(s):
        waits.append(s)

    resilient = ResilientLLM(llm, min_interval_s=0.0, sleep=sleep)
    out = await resilient.complete([Message("user", "hi")])
    assert out.text == "from a" and clients["a"].calls == 2 and clients["b"].calls == 0
    assert 2.0 <= waits[0] <= 3.0 and llm.model_switches == 0


async def test_503_gets_one_quick_retry_then_succeeds_on_same_model():
    busy = TransientLLMError("503", unavailable=True)
    llm, clients, sleeps = make_failover({"a": [busy], "b": []})
    out = await llm.complete([Message("user", "hi")])
    assert out.text == "from a" and clients["a"].calls == 2 and clients["b"].calls == 0
    assert sleeps == [llm.quick_retry_s]


async def test_persistent_503_fails_over_and_cools_the_model_down():
    busy = lambda: TransientLLMError("503", unavailable=True)  # noqa: E731
    llm, clients, _ = make_failover({"a": [busy(), busy()], "b": []})
    out = await llm.complete([Message("user", "hi")])
    assert out.text == "from b" and clients["a"].calls == 2
    assert llm.health.reason("a") == "unavailable"


async def test_404_model_is_skipped_for_the_day():
    llm, clients, _ = make_failover({"a": [ModelUnavailableError("nope", model="a")], "b": []})
    assert (await llm.complete([Message("user", "hi")])).text == "from b"
    assert llm.health.reason("a") == "missing"


async def test_other_transient_errors_are_left_to_the_resilient_retry():
    llm, clients, _ = make_failover({"a": [TransientLLMError("malformed call")], "b": []})
    with pytest.raises(TransientLLMError):
        await llm.complete([Message("user", "hi")])
    assert clients["a"].calls == 1 and clients["b"].calls == 0


async def test_network_error_never_marks_or_skips_a_model():
    dns = lambda: TransientLLMError("[Errno 11001] getaddrinfo failed", network=True)  # noqa: E731
    llm, clients, sleeps = make_failover({"a": [dns(), dns()], "b": []})
    for _ in range(2):
        with pytest.raises(TransientLLMError) as info:
            await llm.complete([Message("user", "hi")])
        assert info.value.network
    assert clients["a"].calls == 2 and clients["b"].calls == 0  # no quick retry, no failover
    assert llm.health.reason("a") is None and llm.health.reason("b") is None and sleeps == []
    assert (await llm.complete([Message("user", "hi")])).text == "from a" and llm.model_switches == 0


async def test_resilient_over_failover_rides_out_a_dns_outage_on_the_same_model():
    dns = lambda: TransientLLMError("getaddrinfo failed", network=True)  # noqa: E731
    llm, clients, _ = make_failover({"a": [dns(), dns(), dns()], "b": []})
    waits: list[float] = []

    async def sleep(s):
        waits.append(s)

    out = await ResilientLLM(llm, min_interval_s=0.0, sleep=sleep).complete([Message("user", "hi")])
    assert out.text == "from a" and clients["a"].calls == 4 and clients["b"].calls == 0
    assert llm.health.reason("a") is None


async def test_all_models_exhausted_raises_clear_error():
    plan = {m: [QuotaExhaustedError("x", model=m)] for m in ("a", "b")}
    llm, _, _ = make_failover(plan)
    with pytest.raises(QuotaExhaustedError) as info:
        await llm.complete([Message("user", "hi")])
    assert str(info.value).startswith(ALL_EXHAUSTED_MESSAGE)
    assert "GEMINI_MODELS" in str(info.value) and "tomorrow" in str(info.value)
    # and it keeps failing fast without calling anything
    with pytest.raises(QuotaExhaustedError):
        await llm.complete([Message("user", "hi")])


async def test_all_models_temporarily_overloaded_is_retryable_not_fatal():
    busy = lambda: TransientLLMError("503", unavailable=True)  # noqa: E731
    llm, _, _ = make_failover({"a": [busy(), busy()], "b": [busy(), busy()]})
    with pytest.raises(TransientLLMError) as info:
        await llm.complete([Message("user", "hi")])
    assert not isinstance(info.value, QuotaExhaustedError)


# ---------------------------------------------------------------- signatures across a model switch


class FakeSDK:
    """Stands in for ``genai.Client``: records the contents of every request, replays queued results."""

    def __init__(self, results):
        self.results, self.requests = list(results), []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))

    async def _generate(self, *, model, contents, config):
        self.requests.append({"model": model, "contents": contents, "config": config})
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _call_response(signature=b"real-sig", call_id=None, name="ping", extra=()):
    parts = [*extra, types.Part(function_call=types.FunctionCall(id=call_id, name=name, args={"n": 1}), thought_signature=signature)]
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=parts), finish_reason="STOP")],
        usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=5, candidates_token_count=2),
    )


def _model_parts(request):
    return [p for c in request["contents"] if c.role == "model" for p in c.parts]


async def test_signatures_are_replaced_on_a_model_switch_and_kept_on_the_same_model():
    sdk_a = FakeSDK([_call_response(b"real-sig-A", call_id="srv-1"), _call_response(b"sig-A2", call_id="srv-2"), genai_errors.ClientError(429, _quota_body())])
    sdk_b = FakeSDK([_call_response(b"sig-B")])
    sdk_c = FakeSDK([_call_response(None)])  # a model that does not validate signatures
    clients = {
        "gemini-3.5-flash": GeminiClient("", "gemini-3.5-flash", client=sdk_a),
        "gemini-3.6-flash": GeminiClient("", "gemini-3.6-flash", client=sdk_b),
        "gemini-2.5-flash": GeminiClient("", "gemini-2.5-flash", client=sdk_c),
    }
    llm = FailoverLLM(list(clients), lambda m: clients[m], health=ModelHealth(), sleep=_nosleep)

    messages = [Message("system", "s"), Message("user", "go")]
    r1 = await llm.complete(messages, [SPEC])
    messages += [Message("assistant", "", tool_calls=r1.tool_calls), Message("tool", "pong", tool_call_id=r1.tool_calls[0].id, name="ping")]
    r2 = await llm.complete(messages, [SPEC])  # same model: the real signature of call 1 is echoed
    part = _model_parts(sdk_a.requests[1])[0]
    assert part.thought_signature == b"real-sig-A" and part.function_call.id == "srv-1"

    messages += [Message("assistant", "", tool_calls=r2.tool_calls), Message("tool", "pong", tool_call_id=r2.tool_calls[0].id, name="ping")]
    r3 = await llm.complete(messages, [SPEC])  # model A is out of daily quota -> B takes over mid-run
    assert llm.active_model == "gemini-3.6-flash" and llm.model_switches == 1
    parts_b = _model_parts(sdk_b.requests[0])
    assert len(parts_b) == 2
    for p in parts_b:  # history made by model A: its signatures and ids are dropped, documented dummy used
        assert p.thought_signature == DUMMY_THOUGHT_SIGNATURE and p.function_call.id is None
        assert b"real-sig" not in (p.thought_signature or b"") and p.thought_signature != b"sig-A2"
    responses = [p for c in sdk_b.requests[0]["contents"] for p in c.parts if p.function_response]
    assert all(p.function_response.id is None for p in responses)

    # B's own call keeps B's signature when B continues; a non-Gemini-3 model gets no dummy at all
    assert r3.tool_calls[0].id in clients["gemini-3.6-flash"]._meta
    plain = clients["gemini-2.5-flash"]
    _, contents = to_gemini_contents(
        messages, plain._meta, foreign_signature=plain._foreign_signature
    )
    assert all(p.thought_signature is None for c in contents for p in c.parts if p.function_call)


def _quota_body():
    return {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}


async def _nosleep(_s):
    return None


# ---------------------------------------------------------------- thought summaries


def test_parse_response_separates_thought_summary_from_text():
    raw = _call_response(extra=[types.Part(text="Checking the workspace first.", thought=True), types.Part(text="Visible text")])
    out = parse_response(raw)
    assert out.text == "Visible text" and out.thought == "Checking the workspace first."


async def test_gemini_client_requests_thought_summaries():
    sdk = FakeSDK([_call_response()])
    await GeminiClient("", "gemini-3.5-flash", client=sdk).complete([Message("user", "x")], [SPEC])
    assert sdk.requests[0]["config"].thinking_config.include_thoughts is True
    assert sdk.requests[0]["config"].thinking_config.thinking_budget is None  # budget left at the default


async def test_rejected_thinking_config_is_dropped_and_retried_once():
    bad = genai_errors.ClientError(400, {"error": {"code": 400, "message": "Thought summaries are not supported", "status": "INVALID_ARGUMENT"}})
    sdk = FakeSDK([bad, _call_response()])
    client = GeminiClient("", "gemini-2.5-flash-lite", client=sdk)
    await client.complete([Message("user", "x")], [SPEC])
    assert sdk.requests[1]["config"].thinking_config is None
    assert client._include_thoughts is False  # not asked again on later calls


def test_runner_thought_prefers_text_then_truncated_summary():
    assert AgentRunner._thought_of(ok(text="I will list files.", thought="summary")) == ("I will list files.", False)
    long = "word " * 200
    thought, is_summary = AgentRunner._thought_of(ok(thought=long))
    assert is_summary and len(thought) <= THOUGHT_LIMIT + 1 and thought.endswith("…")
    assert AgentRunner._thought_of(ok()) == ("", True)


def test_summary_thoughts_are_not_echoed_into_the_history():
    call = dict(tool="ping", args={}, ok=True, observation="pong", turn=1, tool_call_id="c1")
    summary = Step(thought="Long reasoning summary", thought_is_summary=True, **call)
    spoken = Step(thought="I said this", thought_is_summary=False, **{**call, "turn": 2, "tool_call_id": "c2"})
    messages = transcript_messages([[summary], [spoken]])
    assistants = [m for m in messages if m.role == "assistant"]
    assert assistants[0].content == "" and assistants[1].content == "I said this"


# ---------------------------------------------------------------- config


def test_model_chain_defaults_and_backward_compatible_single_model():
    default = Settings(_env_file=None)
    assert default.gemini_models[0] == "gemini-3.5-flash" and "gemini-3.8-flash" in default.gemini_models
    explicit = Settings(_env_file=None, GEMINI_MODEL="gemini-2.5-flash")
    assert explicit.gemini_models[0] == "gemini-2.5-flash"
    assert explicit.gemini_models.count("gemini-2.5-flash") == 1  # no duplicate
    custom = Settings(_env_file=None, GEMINI_MODELS=" x , y ,x,,")
    assert custom.gemini_models == ["x", "y"]
