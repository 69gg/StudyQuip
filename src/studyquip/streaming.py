"""Observe SSE without publishing incomplete model results or losing protocol fields."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from openai import AsyncOpenAI
from openai.lib.streaming.chat import ChatCompletionStreamState

Json = dict[str, Any]
Progress = Callable[[Json], Awaitable[None]]


class StreamInterrupted(RuntimeError):
    """The provider closed a stream before its terminal response."""


class StreamProtocolError(StreamInterrupted):
    """Invalid stream metadata cannot be repaired by retrying the same request."""


class StreamProgress:
    def __init__(self, callback: Progress | None, interval: float) -> None:
        self.callback, self.interval = callback, interval
        self.last_saved = 0.0
        self.value: Json = {
            "state": "requesting",
            "streaming": True,
            "at": time.time(),
            "received_events": 0,
            "output_characters": 0,
            "reasoning_characters": 0,
            "tool_argument_characters": 0,
            "tool_names": [],
        }

    async def update(
        self,
        *,
        output: str = "",
        reasoning: str = "",
        arguments: str = "",
        names: list[str] | None = None,
        force: bool = False,
    ) -> None:
        now = time.time()
        self.value["received_events"] += 1
        self.value.setdefault("first_received_at", now)
        self.value["last_received_at"] = now
        self.value["output_characters"] += len(output)
        self.value["reasoning_characters"] += len(reasoning)
        self.value["tool_argument_characters"] += len(arguments)
        if arguments or names:
            self.value["stream_phase"] = "arguments"
        elif output:
            self.value["stream_phase"] = "content"
        elif reasoning:
            self.value["stream_phase"] = "thinking"
        self.value["tool_names"] = list(dict.fromkeys([*self.value["tool_names"], *(names or [])]))
        if self.callback and (force or now - self.last_saved >= self.interval):
            self.last_saved = now
            await self.callback(dict(self.value))


async def stream_response(
    client: AsyncOpenAI, protocol: str, params: Json, progress: StreamProgress, include_usage: bool = True
) -> Json:
    if protocol == "chat":
        options = {"stream_options": {"include_usage": True}} if include_usage else {}
        stream = await client.chat.completions.create(**params, stream=True, **options)
        # Reuse the locked SDK's delta accumulator without its optional JSON auto-parser.
        accumulator = ChatCompletionStreamState()
        received = False
        async with stream:
            async for chunk in stream:
                received = True
                if any(choice.delta.role not in (None, "assistant") for choice in chunk.choices):
                    raise StreamProtocolError("Chat 流式响应的 role 必须为 assistant，未保存异常响应")
                list(accumulator.handle_chunk(chunk))
                # The SDK concatenates every string, including repeated role
                # metadata from compatible providers. Restore this enum after
                # each chunk; actual text, reasoning and tool fragments stay intact.
                for choice in accumulator.current_completion_snapshot.choices:
                    choice.message.role = "assistant"
                deltas = [choice.delta.model_dump(exclude_none=True) for choice in chunk.choices]
                calls = [call for delta in deltas for call in delta.get("tool_calls", [])]
                await progress.update(
                    output="".join(delta.get("content", "") for delta in deltas),
                    reasoning="".join(
                        delta.get("reasoning_content", delta.get("reasoning", "")) for delta in deltas
                    ),
                    arguments="".join(call.get("function", {}).get("arguments", "") for call in calls),
                    names=[
                        call["function"]["name"] for call in calls if call.get("function", {}).get("name")
                    ],
                    force=any(choice.finish_reason for choice in chunk.choices),
                )
        if not received:
            raise StreamInterrupted("流式响应为空；请检查服务商流式能力，或在模型设置中关闭流式请求")
        result = accumulator.current_completion_snapshot.model_dump(mode="json", exclude_none=True)
        if not result.get("choices") or any(not choice.get("finish_reason") for choice in result["choices"]):
            raise StreamInterrupted("流式连接在完整结果返回前中断，未提交部分结果")
        return result

    stream = await client.responses.create(**params, stream=True)
    result: Json | None = None
    async with stream:
        async for event in stream:
            value = event.model_dump(mode="json", exclude_none=True)
            kind, delta = value["type"], value.get("delta", "")
            item = value.get("item", {})
            await progress.update(
                output=delta if kind == "response.output_text.delta" else "",
                reasoning=delta
                if kind in {"response.reasoning_text.delta", "response.reasoning_summary_text.delta"}
                else "",
                arguments=delta if kind == "response.function_call_arguments.delta" else "",
                names=[item["name"]] if item.get("type") == "function_call" and item.get("name") else [],
                force=kind in {"response.completed", "response.incomplete", "response.failed", "error"},
            )
            if kind in {"response.completed", "response.incomplete"}:
                result = value["response"]  # Includes complete reasoning/encrypted items and usage.
            if kind in {"response.failed", "error"}:
                raise StreamInterrupted("服务商报告流式请求失败，未提交部分结果")
    if result is None:
        raise StreamInterrupted("Responses 流式连接未返回终态，未提交部分结果")
    return result
