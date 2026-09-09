"""必要的协议夹具：实际 SDK 解析、工具续接、参数与失败边界。"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from studyquip.ai import (
    AIProtocolError,
    AIService,
    ContextBudgetExceeded,
    ModelProfile,
    rejected_result,
    request_parameters,
)
from studyquip.config import Settings
from studyquip.pipelines import QuestionDraft, RevisionDraft
from studyquip.scheduling import WindowClosed


class FakeProfiles:
    def __init__(self, profiles: list[ModelProfile]) -> None:
        self.profiles = profiles

    def secret(self) -> bytes:
        return b"fixture-application-secret"

    def list(self, kind: str) -> list[dict[str, Any]]:
        return [profile.model_dump() for profile in self.profiles]


class Result(BaseModel):
    answer: str


def tool_response(protocol: str, index: int, name: str, arguments: dict[str, Any]) -> httpx.Response:
    encoded = json.dumps(arguments, ensure_ascii=False)
    if protocol == "chat":
        body: dict[str, Any] = {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "reasoning_content": f"private-reasoning-{index}",
                        "tool_calls": [
                            {
                                "id": f"call-{index}",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": encoded,
                                },
                            }
                        ],
                    },
                }
            ],
        }
    else:
        body = {
            "id": f"response-{index}",
            "status": "completed",
            "output": [
                {"type": "reasoning", "id": f"reason-{index}", "summary": [], "encrypted_content": "opaque"},
                {
                    "type": "function_call",
                    "id": f"item-{index}",
                    "call_id": f"call-{index}",
                    "name": name,
                    "arguments": encoded,
                    "status": "completed",
                },
            ],
        }
    body["usage"] = {"input_tokens": 10, "output_tokens": 20}
    return httpx.Response(200, json=body)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol,store", [("chat", False), ("responses", False), ("responses", True)])
async def test_lookup_errors_leave_final_result_repair_available(
    protocol: str, store: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("studyquip.ai.retry_delay", lambda attempt: 0)
    configured = profile(protocol=protocol, store=store, retries=1, max_tool_rounds=3)
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)
        requests.append(wire)
        index = len(requests)
        if index <= 2:
            return tool_response(protocol, index, "read_block", {"block_id": "missing"})
        if index == 4:
            history = wire["messages" if protocol == "chat" else "input"]
            feedback = history[-1]
            assert feedback.get("tool_call_id", feedback.get("call_id")) == "call-3"
            assert "answer" in feedback.get("content", feedback.get("output"))
        return tool_response(protocol, index, "submit_result", {} if index == 3 else {"answer": "完整正文"})

    async def read(arguments: dict[str, Any]) -> None:
        raise ValueError("块不存在或超出允许范围")

    ai = AIService(FakeProfiles([configured]), Settings(), transport=httpx.MockTransport(respond))
    state: dict[str, Any] = {}
    result = await ai.structured(
        configured,
        "整理本页",
        Result,
        state=state,
        tools={
            "read_block": (
                "读取正文",
                {"type": "object", "properties": {"block_id": {"type": "string"}}},
                read,
            )
        },
    )
    assert result.answer == "完整正文" and len(requests) == 4
    assert state["format_retries"] == 1 and state["tool_errors"] == 2
    assert [event["status"] for event in state["tool_events"]] == ["error", "error", "error", "completed"]
    assert "private-reasoning" not in json.dumps(state["tool_events"])


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [False, True])
async def test_gateway_retry_diagnosis_is_persisted_and_does_not_accept_failed_response(
    recover: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("studyquip.ai.retry_delay", lambda attempt: 0)
    configured = profile(timeout_seconds=12000, retries=2)
    attempts = 0
    snapshots: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if recover and attempts == 3:
            return tool_response("chat", attempts, "submit_result", {"answer": "完成"})
        return httpx.Response(504, json={"error": {"message": "openai_error"}}, headers={"Retry-After": "0"})

    async def save(value: dict[str, Any]) -> None:
        snapshots.append(copy.deepcopy(value))

    ai = AIService(FakeProfiles([configured]), Settings(), transport=httpx.MockTransport(respond))
    state: dict[str, Any] = {}
    if recover:
        assert (await ai.structured(configured, "正文", Result, state=state, save=save)).answer == "完成"
    else:
        with pytest.raises(AIProtocolError, match=r"上游网关超时.*已重试 2/2.*12000 秒"):
            await ai.structured(configured, "正文", Result, state=state, save=save)
        assert "result" not in state and state["activity"]["state"] == "failed"
    assert attempts == 3
    failures = [s["activity"] for s in snapshots if s.get("activity", {}).get("last_failure")]
    assert {item["last_failure"]["status"] for item in failures} == {504}
    assert failures[-1]["last_failure"]["timeout_seconds"] == 12000
    assert failures[-1]["retry_limit"] == 2
    assert "fixture-key" not in json.dumps(failures)


def profile(**values: Any) -> ModelProfile:
    values.setdefault("stream", False)  # Non-stream protocol fixtures; SSE is tested separately.
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
    "configuration",
    [
        {},
        {"context_tokens": None},
        {"context_tokens": 4096},
        {"context_tokens": 16384},
    ],
)
async def test_context_limit_is_only_applied_when_explicit(configuration: dict[str, int | None]) -> None:
    configured = profile(**configuration)
    assert configured.context_tokens == configuration.get("context_tokens")

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
        Settings(),
        transport=httpx.MockTransport(respond),
    )
    # Only an explicitly configured ceiling can prevent a request.
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
@pytest.mark.parametrize("retries", [0, 2])
async def test_invalid_result_uses_configured_retries_and_persists_exhaustion(
    retries: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("studyquip.ai.retry_delay", lambda attempt: 0)
    configured = profile(retries=retries, max_tool_rounds=1)
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
                                    "function": {"name": "submit_result", "arguments": "{invalid json"},
                                }
                            ],
                        },
                    }
                ],
            },
        )

    ai = AIService(FakeProfiles([configured]), Settings(), transport=httpx.MockTransport(respond))
    state: dict[str, Any] = {}
    with pytest.raises(AIProtocolError, match=f"已用完 {retries} 次"):
        await ai.structured(configured, "test", Result, state=state)
    assert count == retries + 1 and state["usage"]["requests"] == count
    assert "result" not in state and not state["pending"] and state["format_failure"]
    # Automatic recovery cannot silently replenish an exhausted allowance.
    with pytest.raises(AIProtocolError, match="已用完"):
        await ai.structured(configured, "test", Result, state=copy.deepcopy(state))
    assert count == retries + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol,store", [("chat", False), ("responses", False), ("responses", True)])
async def test_format_retries_preserve_protocol_feedback_and_window_checkpoint(
    protocol: str, store: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("studyquip.ai.retry_delay", lambda attempt: 0)
    configured = profile(id="editable", protocol=protocol, store=store, retries=1, max_tool_rounds=1)
    profiles = FakeProfiles([configured])
    requests: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    valid = {
        "operations": [
            {
                "op": "insert",
                "block": {
                    "node_id": "node",
                    "text": "原文",
                    "source_page_ids": ["page"],
                },
            }
        ],
        "reason": "整理",
        "working_summary": "摘要",
        "concepts": [{"name": "概念", "aliases": ["别名"], "evidence": []}],
    }
    invalid = copy.deepcopy(valid)
    invalid["operations"][0]["block"]["source_page_ids"] = {"item": "page", "unknown": True}
    invalid["concepts"][0]["aliases"] = {"item": ["别名"], "unknown": True}
    invalid["concepts"][0]["evidence"] = {"item": [], "unknown": True}
    with pytest.raises(ValidationError) as failure:
        RevisionDraft.model_validate(invalid)
    assert failure.value.error_count() == 3
    # Keep the provider-facing schema compatible with the prior anyOf definition.
    operation_schema = RevisionDraft.model_json_schema()["properties"]["operations"]["items"]
    assert (
        "anyOf" in operation_schema
        and "oneOf" not in operation_schema
        and "discriminator" not in operation_schema
    )

    def respond(request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)
        requests.append(wire)
        count = len(requests)
        arguments = json.dumps(invalid if count == 2 else valid, ensure_ascii=False)
        if protocol == "chat":
            message: dict[str, Any] = {"role": "assistant", "reasoning_content": f"thinking-{count}"}
            if count == 1:
                message["content"] = "已整理完毕。"
            else:
                message["tool_calls"] = [
                    {
                        "id": f"call-{count}",
                        "type": "function",
                        "function": {"name": "submit_result", "arguments": arguments},
                    }
                ]
            body = {
                "choices": [{"finish_reason": "stop" if count == 1 else "tool_calls", "message": message}]
            }
        else:
            item = (
                {
                    "type": "message",
                    "id": "text-1",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "已整理完毕。", "annotations": []}],
                }
                if count == 1
                else {
                    "type": "function_call",
                    "id": f"item-{count}",
                    "call_id": f"call-{count}",
                    "name": "submit_result",
                    "arguments": arguments,
                    "status": "completed",
                }
            )
            body = {
                "id": f"response-{count}",
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "id": f"reason-{count}",
                        "summary": [],
                        "encrypted_content": f"opaque-{count}",
                    },
                    item,
                ],
            }
        body["usage"] = {"input_tokens": 10, "output_tokens": 20}
        return httpx.Response(200, json=body)

    async def save(value: dict[str, Any]) -> None:
        snapshots.append(copy.deepcopy(value))

    async def before() -> None:
        if len(requests) == 1:
            raise WindowClosed(12345)

    ai = AIService(profiles, Settings(), transport=httpx.MockTransport(respond))
    state: dict[str, Any] = {}
    with pytest.raises(WindowClosed):
        await ai.structured(
            configured, "整理原文", RevisionDraft, state=state, save=save, before_request=before
        )
    assert len(requests) == 1 and state["format_retry"]["attempt"] == 1
    assert "result" not in state
    checkpoint = json.loads(json.dumps(snapshots[-1]))
    profiles.profiles = [configured.model_copy(update={"retries": 2, "revision": 2})]
    result = await ai.structured(configured, "不得替换已保存原文", RevisionDraft, state=checkpoint, save=save)
    assert result.operations[0].block.source_page_ids == ["page"]
    assert len(requests) == 3 and checkpoint["format_retries"] == 2
    assert checkpoint["usage"] == {"requests": 3, "input_tokens": 30, "output_tokens": 60}
    assert checkpoint["rounds"] - checkpoint["format_rounds"] == 1
    retry_activities = [s["activity"] for s in snapshots if s.get("activity", {}).get("state") == "retrying"]
    assert {a["format_attempt"] for a in retry_activities} == {1, 2}
    wire_history = requests[-1]["messages" if protocol == "chat" else "input"]
    feedback = wire_history[-1]
    assert feedback.get("tool_call_id", feedback.get("call_id")) == "call-2"
    detail = feedback.get("content", feedback.get("output"))
    assert "source_page_ids" in detail
    assert (
        "aliases" not in detail and "concepts" not in detail
    )  # Optional enrichment no longer blocks repair.
    assert "UpdateOperation" not in detail and "errors.pydantic.dev" not in detail
    if protocol == "chat":
        assert any(item.get("reasoning_content") == "thinking-1" for item in wire_history)
        assert any(
            item.get("role") == "user" and "submit_result" in str(item.get("content"))
            for item in wire_history[2:]
        )
    elif store:
        refs = [item["id"] for item in wire_history if item.get("type") == "item_reference"]
        assert refs == ["reason-1", "text-1", "reason-2", "item-2"]
        assert not any(item.get("type") == "reasoning" for item in wire_history)
    else:
        assert [item["encrypted_content"] for item in wire_history if item.get("type") == "reasoning"] == [
            "opaque-1",
            "opaque-2",
        ]


@pytest.mark.parametrize("encoding", ["item", "json"])
def test_model_array_decoding_preserves_text_and_rejects_ambiguous_values(encoding: str) -> None:
    valid = {
        "operations": [
            {
                "op": "insert",
                "block": {"node_id": "node", "text": '["原样正文"]', "source_page_ids": ["page"]},
            }
        ],
        "concepts": [
            {
                "name": "概念",
                "aliases": ["别名", "另一个别名"],
                "evidence": [{"block_id": "block", "revision": 1, "quote": '["原样正文"]'}],
            }
        ],
        "reason": "整理",
        "working_summary": "",
    }

    def encode(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: encode(item) for key, item in value.items()}
        if isinstance(value, list):
            items = [encode(item) for item in value]
            return (
                {"item": items[0] if len(items) == 1 else items}
                if encoding == "item"
                else json.dumps(items, ensure_ascii=False)
            )
        return value

    raw = encode(valid)
    original = copy.deepcopy(raw)
    assert RevisionDraft.model_validate(raw) == RevisionDraft.model_validate(valid)
    assert raw == original
    question = QuestionDraft(type="short_answer", stem='["正文"]', answer_from_reference='["答案"]')
    assert question.stem == '["正文"]' and question.answer_from_reference == '["答案"]'
    for bad in ("page", "['page']", {"item": "page", "extra": True}, {"item": None}, [None]):
        invalid = copy.deepcopy(valid)
        invalid["operations"][0]["block"]["source_page_ids"] = bad
        with pytest.raises(ValidationError):
            RevisionDraft.model_validate(invalid)
    missing = copy.deepcopy(valid)
    del missing["operations"][0]["op"]
    with pytest.raises(ValidationError, match="union_tag_not_found"):
        RevisionDraft.model_validate(missing)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol,store", [("chat", False), ("responses", False), ("responses", True)])
async def test_resume_revalidates_saved_result_without_another_model_call(
    protocol: str, store: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = profile(protocol=protocol, store=store, retries=0)
    raw = {
        "operations": [
            {
                "op": "insert",
                "block": {"node_id": "node", "text": "正文", "source_page_ids": '["page"]'},
            }
        ],
        "concepts": [{"name": "概念", "aliases": {"item": "别名"}, "evidence": {"item": []}}],
        "reason": "整理",
        "working_summary": "",
    }
    arguments = json.dumps(raw, ensure_ascii=False)
    requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        body = (
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "reasoning_content": "opaque-reasoning",
                            "tool_calls": [
                                {
                                    "id": "submit",
                                    "type": "function",
                                    "function": {"name": "submit_result", "arguments": arguments},
                                }
                            ],
                        },
                    }
                ]
            }
            if protocol == "chat"
            else {
                "id": "response",
                "status": "completed",
                "output": [
                    {"type": "reasoning", "id": "reason", "summary": [], "encrypted_content": "opaque"},
                    {
                        "type": "function_call",
                        "id": "item",
                        "call_id": "submit",
                        "name": "submit_result",
                        "arguments": arguments,
                        "status": "completed",
                    },
                ],
            }
        )
        body["usage"] = {"input_tokens": 10, "output_tokens": 20}
        return httpx.Response(200, json=body)

    ai = AIService(FakeProfiles([configured]), Settings(), transport=httpx.MockTransport(respond))
    state: dict[str, Any] = {}
    validate = RevisionDraft.model_validate

    def legacy_validate(value: Any, **kwargs: Any) -> RevisionDraft:
        # Reproduce the old list[str] validator without changing the wire schema.
        TypeAdapter(list[str]).validate_python(value["operations"][0]["block"]["source_page_ids"])
        return validate(value)

    with monkeypatch.context() as legacy:
        legacy.setattr(RevisionDraft, "model_validate", legacy_validate)
        with pytest.raises(AIProtocolError, match="已用完 0 次"):
            await ai.structured(configured, "整理", RevisionDraft, state=state)
    assert state["rejected_result"]["arguments"] == arguments and requests == 1
    # A failed task still requires explicit resume, even after the parser was upgraded.
    with pytest.raises(AIProtocolError, match="已用完"):
        await ai.structured(configured, "整理", RevisionDraft, state=state)
    if protocol == "chat":
        state.pop("rejected_result")  # Legacy checkpoints only retained the raw transcript.
    history = copy.deepcopy(state["transcript"])
    state.pop("format_failure")  # The resume endpoint clears this only for an explicit continuation.
    checkpoint = json.loads(json.dumps(state))
    result = await ai.structured(configured, "整理", RevisionDraft, state=checkpoint)
    assert result.operations[0].block.source_page_ids == ["page"]
    assert result.concepts[0].aliases == ["别名"]
    assert requests == 1 and checkpoint["usage"] == state["usage"]
    assert checkpoint["transcript"] == history
    assert checkpoint["result_recovered_from_call_id"] == "submit" and not checkpoint["pending"]
    assert "rejected_result" not in checkpoint
    assert await ai.structured(configured, "新的处理单元", RevisionDraft) == result
    assert requests == 2  # The same encoding is accepted on its first response in new units.


def test_legacy_result_recovery_requires_a_completed_isolated_submission() -> None:
    call = {"id": "submit", "function": {"name": "submit_result", "arguments": "{}"}}
    state = {
        "transcript": [
            {"role": "assistant", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "submit", "content": '{"error":"invalid"}'},
        ]
    }
    assert rejected_result(state, "chat")["call_id"] == "submit"
    assert rejected_result({**state, "pending": [call]}, "chat") is None
    assert rejected_result({**state, "rejected_result": None}, "chat") is None
    assert rejected_result(state, "responses") is None
    mismatched = copy.deepcopy(state)
    mismatched["transcript"][-1]["tool_call_id"] = "another-call"
    assert rejected_result(mismatched, "chat") is None
    state["transcript"][0]["tool_calls"].append({"id": "read", "function": {"name": "read_block"}})
    assert rejected_result(state, "chat") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("change", ["configuration", "schema"])
async def test_hot_reload_pins_tool_loop_and_rebuilds_only_unfinished_unit(
    protocol: str, change: str
) -> None:
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
        if change == "configuration":
            profiles.profiles = [
                configured.model_copy(
                    update={
                        "revision": 2,
                        "model": "new-model",
                        "protocol": "responses" if protocol == "chat" else "chat",
                        "api_key": "replacement-key",
                        "thinking": "enabled",
                        "reasoning_effort": "high",
                    }
                )
            ]
        return {"text": "当前原文"}

    ai = AIService(profiles, Settings(), transport=httpx.MockTransport(respond))
    tools = {"read": ("read", {"type": "object", "properties": {}}, read)}
    assert (await ai.structured(configured, "初始单元", Result, tools=tools, state=state)).answer == "完成"
    assert [wire["model"] for wire in requests] == [configured.model, configured.model]
    assert "replacement-key" not in str(state) and configured.api_key not in str(state)
    if change == "schema":
        tools = {"read": ("更新后的读取协议", {"type": "object", "properties": {}}, read)}
    # Resuming a partially persisted unit rebuilds it instead of sending old reasoning/tool items to another API.
    assert (
        await ai.structured(configured, "初始单元", Result, tools=tools, state=interrupted)
    ).answer == "完成"
    if change == "configuration":
        assert requests[-1]["model"] == "new-model"
        assert requests[-1]["thinking"] == {"type": "enabled"}
        assert requests[-1].get("reasoning_effort", requests[-1].get("reasoning", {}).get("effort")) == "high"
        assert ("input" in requests[-1]) is (protocol == "chat")
    else:
        assert requests[-1]["model"] == configured.model
        assert "更新后的读取协议" in json.dumps(requests[-1]["tools"], ensure_ascii=False)
    assert "old-envelope" not in json.dumps(requests[-1])
    assert interrupted["configuration_restarts"] == 1
    assert interrupted["binding"]["revision"] == (2 if change == "configuration" else 1)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol,store", [("chat", False), ("responses", False), ("responses", True)])
async def test_streaming_tools_preserve_complete_protocol_and_observe_deltas(
    protocol: str, store: bool
) -> None:
    configured = profile(protocol=protocol, store=store, stream=True, retries=0)
    wires: list[dict[str, Any]] = []
    observed: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)
        assert wire["stream"] is True
        wires.append(wire)
        number = len(wires)
        name, arguments = (
            ("read", '{"id":"block"}') if number == 1 else ("submit_result", '{"answer":"完成"}')
        )
        if protocol == "chat":
            assert wire["stream_options"] == {"include_usage": True}
            base = {
                "id": f"chat-{number}",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "fixture-model",
            }
            deltas = [
                {"role": "assistant", "reasoning_content": "先读"},
                {"reasoning_content": "依据"},
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"call-{number}",
                            "type": "function",
                            "function": {"name": name, "arguments": arguments[:6]},
                        }
                    ]
                },
                {"tool_calls": [{"index": 0, "function": {"arguments": arguments[6:]}}]},
                {},
            ]
            events = [
                {
                    **base,
                    "choices": [
                        {
                            "index": 0,
                            "delta": delta,
                            "finish_reason": "tool_calls" if i == len(deltas) - 1 else None,
                        }
                    ],
                }
                for i, delta in enumerate(deltas)
            ]
            events.append(
                {
                    **base,
                    "choices": [],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
                }
            )
        else:
            call = {
                "id": f"fc-{number}",
                "type": "function_call",
                "call_id": f"call-{number}",
                "name": name,
                "arguments": arguments,
                "status": "completed",
            }
            result = {
                "id": f"response-{number}",
                "object": "response",
                "created_at": 1,
                "model": "fixture-model",
                "status": "completed",
                "parallel_tool_calls": True,
                "output": [
                    {
                        "id": f"reasoning-{number}",
                        "type": "reasoning",
                        "summary": [],
                        "encrypted_content": "opaque-encrypted",
                    },
                    call,
                ],
                "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
            }
            events = [
                {
                    "type": "response.output_item.added",
                    "output_index": 1,
                    "item": {**call, "arguments": ""},
                    "sequence_number": 1,
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 1,
                    "item_id": call["id"],
                    "delta": arguments,
                    "sequence_number": 2,
                },
                {"type": "response.completed", "response": result, "sequence_number": 3},
            ]
        content = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=content)

    async def read(arguments: dict[str, Any]) -> dict[str, str]:
        assert arguments == {"id": "block"}
        return {"text": "原文"}

    async def save(state: dict[str, Any]) -> None:
        if state.get("activity"):
            observed.append(copy.deepcopy(state["activity"]))

    ai = AIService(FakeProfiles([configured]), Settings(), transport=httpx.MockTransport(respond))
    state: dict[str, Any] = {}
    result = await ai.structured(
        configured,
        "检索后回答",
        Result,
        tools={"read": ("读取", {"type": "object", "properties": {"id": {"type": "string"}}}, read)},
        state=state,
        save=save,
    )
    assert result.answer == "完成" and len(wires) == 2
    assert state["usage"] == {"requests": 2, "input_tokens": 40, "output_tokens": 10}
    assert any(item.get("tool_argument_characters", 0) >= len('{"id":"block"}') for item in observed)
    assert any(item.get("last_received_at") for item in observed)
    if protocol == "chat":
        assert wires[1]["messages"][2]["reasoning_content"] == "先读依据"
        assert wires[1]["messages"][3]["tool_call_id"] == "call-1"
    elif store:
        assert wires[1]["input"][1:3] == [
            {"type": "item_reference", "id": "reasoning-1"},
            {"type": "item_reference", "id": "fc-1"},
        ]
    else:
        assert wires[1]["input"][1]["encrypted_content"] == "opaque-encrypted"
        assert wires[1]["input"][2]["arguments"] == '{"id":"block"}'


@pytest.mark.asyncio
async def test_stream_interruption_retries_without_accepting_partial_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("studyquip.ai.retry_delay", lambda attempt: 0)
    configured = profile(stream=True, stream_include_usage=False, retries=1)
    requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert "stream_options" not in json.loads(request.content)
        chunk = {
            "id": "stream",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fixture",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls" if requests > 1 else None,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call",
                                "type": "function",
                                "function": {
                                    "name": "submit_result",
                                    "arguments": '{"answer":"第二次完整返回"}'
                                    if requests > 1
                                    else '{"answer":"不能使用"}',
                                },
                            }
                        ],
                    },
                }
            ],
        }
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text="data: " + json.dumps(chunk) + "\n\n"
        )

    state: dict[str, Any] = {}
    service = AIService(FakeProfiles([configured]), Settings(), transport=httpx.MockTransport(respond))
    result = await service.structured(configured, "测试", Result, state=state)
    assert requests == 2 and result.answer == "第二次完整返回"
    assert "不能使用" not in json.dumps(state, ensure_ascii=False)
    assert ModelProfile(base_url="https://fixture.invalid/v1", api_key="fixture", model="m").stream is True
