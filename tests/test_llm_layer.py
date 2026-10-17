"""Tests for the LLM layer (no network)."""
from __future__ import annotations

import pytest
from google.genai import errors as genai_errors
from google.genai import types

from worker.llm.base import (
    LLMError,
    LLMResponse,
    MalformedCallError,
    Message,
    RateLimitError,
    ToolCall,
    ToolSpec,
    TransientLLMError,
    Usage,
)
from worker.llm.fake import ScriptedLLM
from worker.llm.gemini import (
    CallMeta,
    decode_json_fields,
    free_form_paths,
    map_error,
    parse_response,
    sanitize_schema,
    sanitize_schema_with_paths,
    to_function_declaration,
    to_gemini_contents,
)
from worker.llm.ollama import parse_ollama_response, to_ollama_messages
from worker.llm.resilient import ResilientLLM, validate_tool_calls

SPEC = ToolSpec(
    name="add",
    description="Add two ints",
    parameters={
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
        "additionalProperties": False,
    },
)


def resp(text="", calls=(), inp=10, out=5) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=list(calls), usage=Usage(inp, out))


# ---------------------------------------------------------------- Gemini conversion


def test_sanitize_schema_strips_unsupported_keys_recursively():
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "default": {"type": "string", "default": "x", "description": "a param named default"},
            "items": {"type": "array", "items": {"type": "object", "additionalProperties": True, "properties": {"n": {"type": "integer", "default": 1}}}},
            "maybe": {"type": ["string", "null"]},
            "mode": {"const": "fast", "type": "string"},
        },
        "required": ["default"],
    }
    out = sanitize_schema(schema)
    assert "$schema" not in out and "additionalProperties" not in out
    assert set(out["properties"]) == {"default", "items", "maybe", "mode"}
    assert "default" not in out["properties"]["default"]
    assert out["properties"]["default"]["description"] == "a param named default"
    inner = out["properties"]["items"]["items"]
    assert "additionalProperties" not in inner and "default" not in inner["properties"]["n"]
    assert out["properties"]["maybe"] == {"type": "string", "nullable": True}
    assert out["properties"]["mode"]["enum"] == ["fast"]
    assert out["required"] == ["default"]


def test_function_declaration_accepted_by_sdk_and_noarg_tools_omit_parameters():
    decl = to_function_declaration(SPEC)
    assert decl.name == "add" and decl.parameters.required == ["a", "b"]
    noarg = to_function_declaration(ToolSpec("ping", "ping", {"type": "object", "properties": {}}))
    assert noarg.parameters is None


def test_to_gemini_contents_maps_roles_and_function_parts():
    sig = b"sig-bytes"
    meta = {"c1": CallMeta(api_id="api-1", thought_signature=sig)}
    messages = [
        Message("system", "be helpful"),
        Message("user", "add 1 and 2"),
        Message("assistant", "ok", tool_calls=[ToolCall("c1", "add", {"a": 1, "b": 2}), ToolCall("c2", "add", {"a": 3, "b": 4})]),
        Message("tool", "3", tool_call_id="c1", name="add"),
        Message("tool", "7", tool_call_id="c2"),  # name resolved from the assistant call
        Message("user", "thanks"),
    ]
    system, contents = to_gemini_contents(messages, meta)

    assert system == "be helpful"
    assert [c.role for c in contents] == ["user", "model", "user", "user"]
    model_parts = contents[1].parts
    assert model_parts[0].text == "ok"
    assert model_parts[1].function_call.name == "add" and model_parts[1].function_call.args == {"a": 1, "b": 2}
    assert model_parts[1].function_call.id == "api-1" and model_parts[1].thought_signature == sig
    assert model_parts[2].function_call.id is None
    responses = contents[2].parts
    assert [p.function_response.name for p in responses] == ["add", "add"]
    assert [p.function_response.response for p in responses] == [{"result": "3"}, {"result": "7"}]
    assert responses[0].function_response.id == "api-1"
    assert contents[3].parts[0].text == "thanks"


def _gemini_response(parts, finish="STOP"):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=parts), finish_reason=finish)],
        usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=12, candidates_token_count=4, thoughts_token_count=3),
    )


def test_parse_response_extracts_text_calls_signatures_and_usage():
    meta: dict[str, CallMeta] = {}
    raw = _gemini_response(
        [
            types.Part(text="thinking...", thought=True),
            types.Part(text="Calling now."),
            types.Part(function_call=types.FunctionCall(name="add", args={"a": 1, "b": 2}), thought_signature=b"s1"),
            types.Part(function_call=types.FunctionCall(id="srv-9", name="add", args={"a": 5, "b": 6})),
        ]
    )
    out = parse_response(raw, meta)
    assert out.text == "Calling now."
    assert [c.name for c in out.tool_calls] == ["add", "add"]
    generated, from_api = out.tool_calls
    assert generated.id.startswith("call_") and from_api.id == "srv-9"
    assert meta[generated.id].thought_signature == b"s1" and meta[generated.id].api_id is None
    assert meta["srv-9"].api_id == "srv-9"
    assert out.usage == Usage(input_tokens=12, output_tokens=7)


def test_parse_response_malformed_function_call_is_transient():
    raw = _gemini_response([types.Part(text="")], finish="MALFORMED_FUNCTION_CALL")
    with pytest.raises(MalformedCallError) as info:
        parse_response(raw)
    assert isinstance(info.value, TransientLLMError)


def test_parse_response_malformed_function_call_without_content_is_still_malformed():
    # Gemini usually sends no content at all with this finish reason; it must not become a fatal "no content" error.
    raw = types.GenerateContentResponse(
        candidates=[types.Candidate(content=None, finish_reason="MALFORMED_FUNCTION_CALL")])
    with pytest.raises(MalformedCallError):
        parse_response(raw)


def test_parse_response_no_content_for_another_reason_stays_a_plain_error():
    raw = types.GenerateContentResponse(candidates=[types.Candidate(content=None, finish_reason="SAFETY")])
    with pytest.raises(LLMError) as info:
        parse_response(raw)
    assert not isinstance(info.value, MalformedCallError)


def test_map_error_429_carries_retry_delay_hint():
    body = {
        "error": {
            "code": 429,
            "message": "quota",
            "status": "RESOURCE_EXHAUSTED",
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "13s"}],
        }
    }
    mapped = map_error(genai_errors.ClientError(429, body))
    assert isinstance(mapped, RateLimitError) and mapped.retry_after == 13.0
    assert isinstance(map_error(genai_errors.ServerError(503, {"error": {"message": "overloaded"}})), TransientLLMError)
    assert not isinstance(map_error(genai_errors.ClientError(400, {"error": {"message": "bad"}})), (RateLimitError, TransientLLMError))


def test_ollama_conversion_roundtrip():
    msgs = [
        Message("user", "hi"),
        Message("assistant", "", tool_calls=[ToolCall("c1", "add", {"a": 1, "b": 2})]),
        Message("tool", "3", tool_call_id="c1", name="add"),
    ]
    converted = to_ollama_messages(msgs)
    assert converted[1]["tool_calls"][0]["function"] == {"name": "add", "arguments": {"a": 1, "b": 2}}
    assert converted[2]["tool_name"] == "add"
    parsed = parse_ollama_response(
        {
            "message": {"content": "", "tool_calls": [{"function": {"name": "add", "arguments": {"a": 1, "b": 2}}}]},
            "prompt_eval_count": 8,
            "eval_count": 3,
        }
    )
    assert parsed.tool_calls[0].name == "add" and parsed.usage == Usage(8, 3)


# ---------------------------------------------------------------- ResilientLLM


class FlakyLLM(ScriptedLLM):
    """Raises the queued exceptions first, then behaves like ScriptedLLM."""

    def __init__(self, errors, script):
        super().__init__(script)
        self.errors = list(errors)

    async def complete(self, messages, tools=None, **kwargs):
        if self.errors:
            self.calls.append({"messages": messages})
            raise self.errors.pop(0)
        return await super().complete(messages, tools, **kwargs)


def make_resilient(inner, **kwargs):
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    llm = ResilientLLM(inner, min_interval_s=kwargs.pop("min_interval_s", 0.0), sleep=fake_sleep, **kwargs)
    return llm, sleeps


async def test_retry_on_rate_limit_honors_hint_then_succeeds():
    inner = FlakyLLM([RateLimitError("429", retry_after=7.0), TransientLLMError("503")], [resp("done")])
    llm, sleeps = make_resilient(inner)
    out = await llm.complete([Message("user", "hi")])
    assert out.text == "done"
    assert llm.stats == {"calls": 1, "input_tokens": 10, "output_tokens": 5, "retries": 2}
    assert 7.0 <= sleeps[0] <= 8.0  # server hint plus <=1s jitter
    assert 0 < sleeps[1] <= 4.0  # exponential backoff with jitter (base 2s * 2^1 * [0.5, 1])


async def test_retries_exhausted_reraises():
    inner = FlakyLLM([RateLimitError("429")] * 10, [])
    llm, _ = make_resilient(inner, max_retries=2)
    with pytest.raises(RateLimitError):
        await llm.complete([Message("user", "hi")])
    assert llm.retries == 2 and llm.calls == 0


class TemperatureProbe(FlakyLLM):
    """FlakyLLM that remembers the temperature of every attempt."""

    def __init__(self, errors, script):
        super().__init__(errors, script)
        self.temperatures: list[float] = []

    async def complete(self, messages, tools=None, *, temperature=0.2, **kwargs):
        self.temperatures.append(temperature)
        return await super().complete(messages, tools, temperature=temperature, **kwargs)


async def test_malformed_call_is_reasked_with_a_rising_temperature_and_its_own_budget():
    inner = TemperatureProbe([MalformedCallError(), MalformedCallError()], [resp("fine")])
    llm, sleeps = make_resilient(inner, max_retries=0)  # the ordinary retry budget is zero: malformed has its own
    out = await llm.complete([Message("user", "hi")], temperature=0.2)
    assert out.text == "fine" and inner.temperatures == pytest.approx([0.2, 0.5, 0.8])
    assert llm.stats["retries"] == 2 and llm.calls == 1 and sleeps == []


async def test_malformed_call_gives_up_after_two_reasks_and_caps_the_temperature():
    inner = TemperatureProbe([MalformedCallError() for _ in range(10)], [])
    llm, _ = make_resilient(inner, max_retries=5)
    with pytest.raises(MalformedCallError):
        await llm.complete([Message("user", "hi")], temperature=0.9)
    assert inner.temperatures == pytest.approx([0.9, 1.0, 1.0]) and llm.retries == 2


async def test_malformed_retries_do_not_use_up_the_other_retry_budget():
    inner = TemperatureProbe([MalformedCallError(), RateLimitError("429"), MalformedCallError()], [resp("ok")])
    llm, _ = make_resilient(inner, max_retries=1)
    assert (await llm.complete([Message("user", "hi")])).text == "ok"
    assert llm.retries == 3


async def test_min_interval_throttle():
    now = [100.0]

    async def fake_sleep(s: float) -> None:
        now[0] += s

    llm = ResilientLLM(ScriptedLLM([resp("a"), resp("b")]), min_interval_s=4.0, sleep=fake_sleep, clock=lambda: now[0])
    await llm.complete([Message("user", "1")])
    now[0] += 1.0  # only 1s passed since the first call started
    t_before = now[0]
    await llm.complete([Message("user", "2")])
    assert now[0] - t_before == pytest.approx(3.0)


def test_validate_tool_calls_detects_unknown_tool_and_bad_args():
    problems = validate_tool_calls(
        [ToolCall("1", "add", {"a": 1, "b": 2}), ToolCall("2", "add", {"a": "x"}), ToolCall("3", "nope", {})], [SPEC]
    )
    assert set(problems) == {"2", "3"}
    assert "b" in problems["2"] or "required" in problems["2"]
    assert "Unknown tool" in problems["3"]


async def test_invalid_args_reprompt_once_then_accept_fixed_call():
    bad = resp(calls=[ToolCall("c1", "add", {"a": "one"})], inp=10, out=5)
    good = resp(calls=[ToolCall("c2", "add", {"a": 1, "b": 2})], inp=20, out=6)
    inner = ScriptedLLM([bad, good])
    llm, _ = make_resilient(inner)
    out = await llm.complete([Message("user", "add")], [SPEC])

    assert out.tool_calls[0].args == {"a": 1, "b": 2}
    assert out.usage == Usage(30, 11)
    reprompt = inner.calls[1]["messages"]
    assert reprompt[-2].role == "assistant" and reprompt[-2].tool_calls[0].id == "c1"
    assert reprompt[-1].role == "tool" and reprompt[-1].tool_call_id == "c1" and reprompt[-1].content.startswith("ERROR")
    assert llm.calls == 2


async def test_invalid_args_returned_as_is_after_single_reprompt():
    bad1 = resp(calls=[ToolCall("c1", "add", {})])
    bad2 = resp(calls=[ToolCall("c2", "ghost", {})])
    inner = ScriptedLLM([bad1, bad2])
    llm, _ = make_resilient(inner)
    out = await llm.complete([Message("user", "add")], [SPEC])
    assert out.tool_calls[0].name == "ghost"
    assert len(inner.calls) == 2  # exactly one re-prompt, no loop


async def test_valid_calls_and_text_only_responses_pass_through_untouched():
    inner = ScriptedLLM([resp(calls=[ToolCall("c1", "add", {"a": 1, "b": 2})]), resp("just text")])
    llm, _ = make_resilient(inner)
    assert (await llm.complete([Message("user", "x")], [SPEC])).tool_calls[0].id == "c1"
    assert (await llm.complete([Message("user", "y")], [SPEC])).text == "just text"
    assert len(inner.calls) == 2 and llm.stats["calls"] == 2


async def test_scripted_llm_callable_and_exhaustion():
    llm = ScriptedLLM(lambda messages, tools: resp(f"echo:{messages[-1].content}"))
    assert (await llm.complete([Message("user", "hi")])).text == "echo:hi"
    assert len(llm.calls) == 1
    empty = ScriptedLLM([])
    with pytest.raises(AssertionError):
        await empty.complete([Message("user", "hi")])


# ---------------------------------------------------------------- Gemini schema compatibility


def _walk(schema):
    """Yield every schema node of a sanitized schema."""
    if isinstance(schema, dict):
        yield schema
        for sub in (schema.get("properties") or {}).values():
            yield from _walk(sub)
        for key in ("items", "anyOf"):
            if key in schema:
                yield from _walk(schema[key])
    elif isinstance(schema, list):
        for sub in schema:
            yield from _walk(sub)


def test_free_form_object_becomes_json_string_and_is_decoded_back():
    spec = ToolSpec(
        "http",
        "x",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "headers": {"type": "object", "description": "Optional request headers."},
                "nested": {"type": "array", "items": {"type": "object", "properties": {"meta": {"type": "object"}}}},
            },
            "required": ["url"],
        },
    )
    out, paths = sanitize_schema_with_paths(spec.parameters)
    assert out["properties"]["headers"] == {
        "type": "string",
        "description": "Optional request headers. (JSON-encoded object)",
    }
    assert out["properties"]["nested"]["items"]["properties"]["meta"]["type"] == "string"
    assert sorted(paths) == [("headers",), ("nested", "*", "meta")]
    assert free_form_paths(spec) == paths
    decoded = decode_json_fields(
        {"url": "u", "headers": '{"A": "b"}', "nested": [{"meta": '{"k": 1}'}, {"meta": "not json"}]}, paths
    )
    assert decoded["headers"] == {"A": "b"}
    assert decoded["nested"][0]["meta"] == {"k": 1}
    assert decoded["nested"][1]["meta"] == "not json"  # lenient: left untouched
    assert decode_json_fields({"url": "u"}, paths) == {"url": "u"}  # absent fields are fine
    assert to_function_declaration(spec).parameters is not None  # accepted by the SDK types


def test_parse_response_decodes_json_string_args():
    spec = ToolSpec("http", "x", {"type": "object", "properties": {"json_body": {"type": "object"}, "url": {"type": "string"}}})
    raw = _gemini_response(
        [types.Part(function_call=types.FunctionCall(name="http", args={"url": "u", "json_body": '{"amount": 5}'}))]
    )
    out = parse_response(raw, None, {"http": free_form_paths(spec)})
    assert out.tool_calls[0].args == {"url": "u", "json_body": {"amount": 5}}


def test_sanitizer_fixes_other_gemini_incompatibilities():
    schema = {
        "type": "object",
        "properties": {
            "tags": {"type": "array"},
            "level": {"type": "integer", "enum": [1, 2]},
            "when": {"type": "string", "format": "uri"},
            "maybe": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["tags", "ghost"],
    }
    out = sanitize_schema(schema)
    assert out["properties"]["tags"]["items"] == {"type": "string"}
    assert out["properties"]["level"] == {"type": "string", "enum": ["1", "2"]}
    assert "format" not in out["properties"]["when"]
    assert out["required"] == ["tags"]


def test_all_registered_tool_specs_are_gemini_compatible():
    from worker.agent.toolset import build_registry
    from worker.config import Settings

    registry = build_registry(Settings(LLM_PROVIDER="fake"))
    for spec in registry.specs():
        params = sanitize_schema(spec.parameters)
        for node in _walk(params):
            if node.get("type") == "object" and node is not params:
                assert node.get("properties"), f"{spec.name}: object without properties"
            if node.get("type") == "array":
                assert "items" in node, f"{spec.name}: array without items"
            if "enum" in node:
                assert node.get("type") == "string" and all(isinstance(v, str) for v in node["enum"])
        to_function_declaration(spec)


# ---------------------------------------------------------------- network outages (time-budgeted retries)


import socket  # noqa: E402

import httpx  # noqa: E402

from worker.llm.base import is_network_error  # noqa: E402


def dns_error() -> TransientLLMError:
    return TransientLLMError("Gemini network error: [Errno 11001] getaddrinfo failed", network=True)


class FakeTime:
    """Injectable clock + sleep: sleeping advances the clock, so budgets are testable without waiting."""

    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def resilient_with_time(inner, **kwargs):
    t = FakeTime()
    llm = ResilientLLM(inner, min_interval_s=0.0, sleep=t.sleep, clock=t.clock, **kwargs)
    return llm, t


async def test_dns_outage_for_a_few_attempts_then_success():
    inner = FlakyLLM([dns_error(), dns_error(), dns_error()], [resp("back online")])
    waiting: list[dict] = []
    llm, t = resilient_with_time(inner, max_retries=1, on_retry=waiting.append)  # max_retries must not limit network waits
    out = await llm.complete([Message("user", "hi")])
    assert out.text == "back online" and llm.retries == 3
    assert len(waiting) == 3 and all(w["kind"] == "network" and w["budget_s"] == 300.0 for w in waiting)
    assert [w["attempt"] for w in waiting] == [1, 2, 3]
    assert all(0 < s <= 30.0 for s in t.sleeps)


async def test_network_retries_use_capped_backoff_and_survive_more_than_five_attempts():
    inner = FlakyLLM([dns_error() for _ in range(10)], [resp("ok")])
    llm, t = resilient_with_time(inner)
    assert (await llm.complete([Message("user", "hi")])).text == "ok"
    assert len(t.sleeps) == 10 and max(t.sleeps) <= 30.0
    assert sum(t.sleeps) < 300.0 and t.sleeps[-1] >= 15.0  # settled at the cap (30s * [0.5, 1])


async def test_network_outage_beyond_the_budget_raises():
    inner = FlakyLLM([dns_error() for _ in range(1000)], [])
    llm, t = resilient_with_time(inner, network_retry_s=120.0)
    with pytest.raises(TransientLLMError) as info:
        await llm.complete([Message("user", "hi")])
    assert info.value.network and "getaddrinfo" in str(info.value)
    assert 120.0 <= t.now - 1000.0 <= 125.0  # waited the whole budget, never past it
    assert llm.calls == 0


async def test_non_network_transient_errors_keep_the_attempt_limit():
    inner = FlakyLLM([TransientLLMError("500") for _ in range(10)], [])
    llm, _ = resilient_with_time(inner, max_retries=2)
    with pytest.raises(TransientLLMError):
        await llm.complete([Message("user", "hi")])
    assert llm.retries == 2


async def test_on_retry_exception_aborts_the_wait():
    class Boom(Exception):
        pass

    def stop(info):
        raise Boom()

    llm, _ = resilient_with_time(FlakyLLM([dns_error()], [resp("x")]), on_retry=stop)
    with pytest.raises(Boom):
        await llm.complete([Message("user", "hi")])


def test_is_network_error_classification():
    assert is_network_error(socket.gaierror(11001, "getaddrinfo failed"))
    assert is_network_error(OSError(11001, "getaddrinfo failed"))
    assert is_network_error(httpx.ConnectError("[Errno 11001] getaddrinfo failed"))
    assert is_network_error(httpx.ConnectTimeout("timed out"))
    assert is_network_error(ConnectionRefusedError(10061, "refused"))
    assert is_network_error(ConnectionResetError())
    wrapped = RuntimeError("wrapper")
    wrapped.__cause__ = httpx.ConnectError("boom")
    assert is_network_error(wrapped)
    assert not is_network_error(httpx.ReadTimeout("slow model"))
    assert not is_network_error(TimeoutError("slow"))
    assert not is_network_error(ValueError("bad"))


def test_map_error_marks_connectivity_errors_as_network():
    for err in (httpx.ConnectError("[Errno 11001] getaddrinfo failed"), socket.gaierror(11001, "getaddrinfo failed"),
                OSError(11001, "getaddrinfo failed"), httpx.ConnectTimeout("t"), ConnectionResetError("reset")):
        mapped = map_error(err, "m")
        assert isinstance(mapped, TransientLLMError) and mapped.network and not mapped.unavailable, err
    read_timeout = map_error(httpx.ReadTimeout("slow"), "m")
    assert isinstance(read_timeout, TransientLLMError) and not read_timeout.network
    assert not isinstance(map_error(ValueError("x"), "m"), TransientLLMError)
    assert isinstance(map_error(ValueError("x"), "m"), LLMError)
