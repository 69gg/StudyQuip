"""必要的协议夹具：实际 SDK 解析、工具续接、参数与失败边界。"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from studyquip.ai import AIProtocolError, AIService, ContextBudgetExceeded, ModelProfile, request_parameters
from studyquip.config import Settings


class FakeProfiles:
    def __init__(self, profiles: list[ModelProfile]) -> None:
        self.profiles = profiles

    def secret(self) -> bytes:
        return b"fixture-application-secret"

    def list(self, kind: str) -> list[dict[str, Any]]:
        return [profile.model_dump() for profile in self.profiles]


class Result(BaseModel):
    answer: str


def profile(**values: Any) -> ModelProfile:
    return ModelProfile(
        base_url="https://provider.example/v1", api_key="fixture-key", model="fixture-model", **values
    )


@pytest.mark.parametrize("thinking", ["omit", "enabled", "disabled"])
def test_parameter_mapping_and_extra_conflicts(thinking: str) -> None:
    chat = profile(thinking=thinking, reasoning_effort="max", extra_body={"seed": 7})
    wire = request_parameters(chat)
    assert wire["reasoning_effort"] == "max"
    assert wire["extra_body"].get("thinking") == (None if thinking == "omit" else {"type": thinking})
    responses = profile(
        protocol="responses", reasoning_effort="high", extra_body={"reasoning": {"summary": "auto"}}
    )
    assert request_parameters(responses)["reasoning"] == {"effort": "high", "summary": "auto"}
    assert request_parameters(responses)["store"] is False
    for invalid in (
        {"model": "override"},
        {"thinking": {"type": "disabled"}},
        {"tool_choice": "auto"},
        {"context_tokens": 4096},
        {"reasoning": {"effort": "low"}},
    ):
        with pytest.raises(ValidationError):
            profile(extra_body=invalid)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocol,store,tool_choice,max_tokens_field,output_configuration",
    [
        ("responses", False, "required", "max_completion_tokens", {}),
        ("responses", True, "required", "max_completion_tokens", {"max_output_tokens": 2048}),
        ("chat", False, "required", "max_completion_tokens", {}),
        ("responses", False, "omit", "max_completion_tokens", {"max_output_tokens": None}),
        ("chat", False, "omit", "max_tokens", {"max_output_tokens": None}),
        ("chat", False, "auto", "max_tokens", {"max_output_tokens": 2048}),
        ("chat", False, "required", "max_completion_tokens", {"max_output_tokens": None}),
        ("chat", False, "required", "max_completion_tokens", {"max_output_tokens": 2048}),
        ("chat", False, "required", "max_tokens", {}),
    ],
)
async def test_sdk_tool_loop_preserves_reasoning_items_and_phase(
    protocol: str,
    store: bool,
    tool_choice: str,
    max_tokens_field: str,
    output_configuration: dict[str, int | None],
) -> None:
    configured = profile(
        protocol=protocol,
        store=store,
        tool_choice=tool_choice,
        thinking="enabled",
        reasoning_effort="max",
        max_tokens_field=max_tokens_field,
        context_tokens=None if tool_choice == "omit" else 8192,
        **output_configuration,
    )
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)
        if tool_choice == "omit":
            assert "tool_choice" not in wire
        else:
            assert wire["tool_choice"] == tool_choice
        assert wire["tools"]
        assert "context_tokens" not in wire
        assert wire["thinking"] == {"type": "enabled"}
        output_fields = {"max_tokens", "max_completion_tokens", "max_output_tokens"}
        limit = output_configuration.get("max_output_tokens")
        expected_output = (
            {max_tokens_field if protocol == "chat" else "max_output_tokens": limit}
            if limit is not None
            else {}
        )
        assert {key: wire[key] for key in output_fields & wire.keys()} == expected_output
        requests.append(wire)
        final = len(requests) > 1
        name, arguments = ("submit_result", '{"answer":"完成"}') if final else ("read", '{"id":"block"}')
        if protocol == "chat":
            return httpx.Response(
                200,
                json={
                    "id": "chat_1",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "fixture-model",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "reasoning_content": "vendor opaque reasoning",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": f"call_{len(requests)}",
                                        "type": "function",
                                        "function": {"name": name, "arguments": arguments},
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
                },
            )
        items: list[dict[str, Any]] = [
            {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "opaque-secret-envelope"},
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "status": "completed",
                "content": [{"type": "output_text", "text": "读取依据", "annotations": []}],
            },
            {
                "id": "fc_1",
                "type": "function_call",
                "call_id": f"call_{len(requests)}",
                "name": name,
                "arguments": arguments,
                "status": "completed",
            },
        ]
        return httpx.Response(
            200,
            json={
                "id": f"resp_{len(requests)}",
                "object": "response",
                "created_at": 1,
                "model": "fixture-model",
                "status": "completed",
                "parallel_tool_calls": True,
                "output": items,
                "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
            },
        )

    async def read(arguments: dict[str, Any]) -> dict[str, Any]:
        assert arguments == {"id": "block"}
        return {"text": "原文"}

    ai = AIService(FakeProfiles([configured]), Settings(), transport=httpx.MockTransport(respond))
    state: dict[str, Any] = {}
    result = await ai.structured(
        configured,
        "test",
        Result,
        images=["data:image/png;base64,private-input-fixture"],
        tools={
            "read": (
                "read",
                {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
                read,
            )
        },
        state=state,
    )
    assert result.answer == "完成"
    assert state["request_context"]["image_hashes"]
    assert "private-input-fixture" not in json.dumps(state)
    assert state["usage"] == {"requests": 2, "input_tokens": 40, "output_tokens": 10}
    if protocol == "chat":
        assert requests[1]["messages"][2]["reasoning_content"] == "vendor opaque reasoning"
        assert requests[1]["messages"][3]["tool_call_id"] == "call_1"
    else:
        continuation = requests[1]["input"][1:]
        if store:
            assert continuation[:3] == [
                {"type": "item_reference", "id": value} for value in ("rs_1", "msg_1", "fc_1")
            ]
        else:
            assert continuation[0]["encrypted_content"] == "opaque-secret-envelope"
            assert continuation[1]["phase"] == "commentary"
            assert continuation[2]["call_id"] == "call_1"
        assert continuation[-1]["type"] == "function_call_output"
    # A persisted completed stage resumes without spending a second model call.
    assert (await ai.structured(configured, "test", Result, state=state)).answer == "完成"
    assert len(requests) == 2


@pytest.mark.parametrize("limit", [0, -1])
def test_maximum_output_rejects_nonpositive_limits(limit: int) -> None:
    with pytest.raises(ValidationError, match="max_output_tokens"):
        profile(max_output_tokens=limit)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configuration,expected",
    [
        ({}, 8192),
        ({"context_tokens": None}, 8192),
        ({"context_tokens": 4096}, 4096),
        ({"context_tokens": 16384}, 8192),
    ],
)
async def test_processing_budget_is_separate_from_optional_request_limit(
    configuration: dict[str, int | None], expected: int
) -> None:
    configured = profile(**configuration)
    assert configured.context_tokens == configuration.get("context_tokens")
    assert configured.effective_context_tokens(8192) == expected

    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)
        assert "context_tokens" not in wire
        requests.append(wire)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "submit",
                                    "type": "function",
                                    "function": {
                                        "name": "submit_result",
                                        "arguments": '{"answer":"完成"}',
                                    },
                                }
                            ],
                        },
                    }
                ]
            },
        )

    ai = AIService(
        FakeProfiles([configured]),
        Settings(context_tokens=8192),
        transport=httpx.MockTransport(respond),
    )
    # The complete request exceeds the material budget, but fits an explicit 16k limit.
    if configured.context_tokens == 4096:
        with pytest.raises(ContextBudgetExceeded, match="4,096"):
            await ai.structured(configured, "测" * 3500, Result)
        assert not requests
    else:
        assert (await ai.structured(configured, "测" * 3500, Result)).answer == "完成"
        assert len(requests) == 1
    if configured.context_tokens is not None:
        count = len(requests)
        with pytest.raises(ContextBudgetExceeded, match="请提高或清空"):
            await ai.structured(configured, "测" * 10000, Result)
        assert len(requests) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat", "responses"])
async def test_unset_request_limit_resumes_large_reasoning_without_truncating(protocol: str) -> None:
    configured = profile(protocol=protocol)
    reasoning = "opaque-reasoning-" * 3000
    transcript = (
        [
            {
                "role": "assistant",
                "content": "读取原文",
                "reasoning_content": reasoning,
                "tool_calls": [
                    {
                        "id": "read-call",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "read-call", "content": "已读取的原文"},
        ]
        if protocol == "chat"
        else [
            {"type": "reasoning", "id": "reasoning-item", "summary": [], "encrypted_content": reasoning},
            {
                "type": "function_call",
                "id": "read-item",
                "call_id": "read-call",
                "name": "read",
                "arguments": "{}",
                "status": "completed",
            },
            {"type": "function_call_output", "call_id": "read-call", "output": "已读取的原文"},
        ]
    )
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)
        requests.append(wire)
        assert "context_tokens" not in wire
        assert (wire["messages"] if protocol == "chat" else wire["input"])[-len(transcript) :] == transcript
        if protocol == "chat":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "submit",
                                        "type": "function",
                                        "function": {
                                            "name": "submit_result",
                                            "arguments": '{"answer":"完成"}',
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": "submit-item",
                        "call_id": "submit",
                        "name": "submit_result",
                        "arguments": '{"answer":"完成"}',
                        "status": "completed",
                    }
                ],
            },
        )

    ai = AIService(FakeProfiles([configured]), Settings(), transport=httpx.MockTransport(respond))
    state: dict[str, Any] = {
        "binding": ai.binding(configured),
        "transcript": copy.deepcopy(transcript),
        "pending": [],
        "rounds": 1,
        "usage": {"requests": 1, "input_tokens": 100, "output_tokens": 200},
    }
    assert (await ai.structured(configured, "当前页只有少量文字", Result, state=state)).answer == "完成"
    assert len(requests) == 1 and state["usage"]["requests"] == 2


@pytest.mark.asyncio
async def test_invalid_result_repairs_once_and_does_not_accept_free_text() -> None:
    configured = profile()
    count = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(
            200,
            json={
                "id": "chat",
                "created": 1,
                "model": "fixture",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": str(count),
                                    "type": "function",
                                    "function": {"name": "submit_result", "arguments": "{}"},
                                }
                            ],
                        },
                    }
                ],
            },
        )

    ai = AIService(FakeProfiles([configured]), Settings(), transport=httpx.MockTransport(respond))
    with pytest.raises(AIProtocolError, match="修复后仍"):
        await ai.structured(configured, "test", Result)
    assert count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat", "responses"])
async def test_hot_reload_pins_tool_loop_and_rebuilds_only_unfinished_unit(protocol: str) -> None:
    configured = profile(id="editable", revision=1, protocol=protocol)
    profiles = FakeProfiles([configured])
    requests: list[dict[str, Any]] = []
    state: dict[str, Any] = {}
    interrupted: dict[str, Any] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)
        requests.append(wire)
        name = "read" if len(requests) == 1 else "submit_result"
        arguments = "{}" if name == "read" else '{"answer":"完成"}'
        if "messages" in wire:
            return httpx.Response(
                200,
                json={
                    "id": "chat",
                    "object": "chat.completion",
                    "created": 1,
                    "model": wire["model"],
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": str(len(requests)),
                                        "type": "function",
                                        "function": {"name": name, "arguments": arguments},
                                    }
                                ],
                            },
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "response",
                "object": "response",
                "created_at": 1,
                "model": wire["model"],
                "status": "completed",
                "output": [
                    {"id": "rs", "type": "reasoning", "summary": [], "encrypted_content": "old-envelope"},
                    {
                        "id": "fc",
                        "type": "function_call",
                        "call_id": str(len(requests)),
                        "name": name,
                        "arguments": arguments,
                        "status": "completed",
                    },
                ],
            },
        )

    async def read(arguments: dict[str, Any]) -> dict[str, str]:
        interrupted.update(copy.deepcopy(state))
        profiles.profiles = [
            configured.model_copy(
                update={
                    "revision": 2,
                    "model": "new-model",
                    "protocol": "responses" if protocol == "chat" else "chat",
                    "api_key": "replacement-key",
                }
            )
        ]
        return {"text": "当前原文"}

    ai = AIService(profiles, Settings(), transport=httpx.MockTransport(respond))
    tools = {"read": ("read", {"type": "object", "properties": {}}, read)}
    assert (await ai.structured(configured, "初始单元", Result, tools=tools, state=state)).answer == "完成"
    assert [wire["model"] for wire in requests] == [configured.model, configured.model]
    assert "replacement-key" not in str(state) and configured.api_key not in str(state)
    # Resuming a partially persisted unit rebuilds it instead of sending old reasoning/tool items to another API.
    assert (
        await ai.structured(configured, "初始单元", Result, tools=tools, state=interrupted)
    ).answer == "完成"
    assert requests[-1]["model"] == "new-model"
    assert ("input" in requests[-1]) is (protocol == "chat")
    assert "old-envelope" not in json.dumps(requests[-1])
    assert interrupted["configuration_restarts"] == 1
    assert interrupted["binding"]["revision"] == 2
    call_count = len(requests)
    await ai.structured(configured, "已完成单元", Result, state=state)
    assert len(requests) == call_count  # Completed business units stay cached.


@pytest.mark.asyncio
async def test_waiting_request_reloads_capacity_without_releasing_existing_slot() -> None:
    configured = profile(id="capacity", max_concurrency=1)
    profiles = FakeProfiles([configured])
    waiting = asyncio.Event()
    ai = AIService(
        profiles,
        Settings(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.5]}]})
        ),
    )

    async def activity(value: dict[str, Any]) -> None:
        if value["state"] == "waiting_capacity":
            waiting.set()

    async with ai.limiter.slot(configured):
        request = asyncio.create_task(ai.embed(configured, ["测试"], activity=activity))
        try:
            await asyncio.wait_for(waiting.wait(), 2)
            assert not request.done()
            profiles.profiles = [configured.model_copy(update={"max_concurrency": 2, "revision": 2})]
            vectors, _ = await asyncio.wait_for(request, 3)
            assert vectors == [[1.0, 0.5]]
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
