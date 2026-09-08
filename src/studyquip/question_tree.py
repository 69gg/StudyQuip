"""Recursive question content, safe figure specifications and shared traversal."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterator
from typing import Any, Literal

from defusedxml.ElementTree import fromstring
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator


class PlotSeries(BaseModel):
    label: str = ""
    expression: str = ""
    points: list[tuple[FiniteFloat, FiniteFloat]] = Field(default_factory=list)


class PlotSpec(BaseModel):
    x_min: FiniteFloat = -5
    x_max: FiniteFloat = 5
    y_min: FiniteFloat = -5
    y_max: FiniteFloat = 5
    x_label: str = "x"
    y_label: str = "y"
    series: list[PlotSeries] = Field(default_factory=list)

    @model_validator(mode="after")
    def bounds(self) -> PlotSpec:
        if self.x_min >= self.x_max or self.y_min >= self.y_max:
            raise ValueError("坐标范围的下界必须小于上界")
        return self


SVG_TAGS = frozenset(
    {
        "svg",
        "g",
        "path",
        "line",
        "polyline",
        "polygon",
        "rect",
        "circle",
        "ellipse",
        "text",
        "tspan",
        "defs",
        "marker",
        "clipPath",
        "title",
        "desc",
    }
)
SVG_ATTRIBUTES = frozenset(
    {
        "id",
        "viewBox",
        "width",
        "height",
        "x",
        "y",
        "x1",
        "x2",
        "y1",
        "y2",
        "cx",
        "cy",
        "r",
        "rx",
        "ry",
        "d",
        "points",
        "fill",
        "fill-rule",
        "stroke",
        "stroke-width",
        "stroke-linecap",
        "stroke-linejoin",
        "stroke-dasharray",
        "opacity",
        "fill-opacity",
        "stroke-opacity",
        "transform",
        "font-size",
        "font-family",
        "font-weight",
        "text-anchor",
        "dominant-baseline",
        "dx",
        "dy",
        "marker-start",
        "marker-mid",
        "marker-end",
        "markerWidth",
        "markerHeight",
        "refX",
        "refY",
        "orient",
        "markerUnits",
        "clip-path",
        "preserveAspectRatio",
    }
)


def validate_svg(value: str) -> str:
    try:
        root = fromstring(value)
    except Exception as error:
        raise ValueError("SVG 不是有效且安全的 XML") from error
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise ValueError("插图必须包含 svg 根元素")
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] not in SVG_TAGS:
            raise ValueError("SVG 仅支持静态图形与文字，不支持脚本、图片外链或交互元素")
        for attribute, content in element.attrib.items():
            if attribute not in SVG_ATTRIBUTES:
                raise ValueError(f"SVG 不支持属性 {attribute}")
            if re.search(r"url\s*\(", content, re.IGNORECASE) and not re.fullmatch(
                r"url\(#[\w.-]+\)", content
            ):
                raise ValueError("SVG 只允许引用图内的标记和裁剪区域")
    return value


class FigureSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: Literal["svg", "plot"]
    title: str = ""
    svg: str | None = None
    plot: PlotSpec | None = None

    @model_validator(mode="after")
    def content(self) -> FigureSpec:
        if self.kind == "svg":
            if not self.svg:
                raise ValueError("SVG 插图缺少图形内容")
            validate_svg(self.svg)
        elif self.plot is None:
            raise ValueError("坐标图缺少绘图配置")
        return self


class Material(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: Literal["text", "listening"] = "text"
    title: str = ""
    text: str = ""
    audio_asset_id: str | None = None
    audio_generated_from: str | None = None


def walk_questions(question: dict[str, Any]) -> Iterator[dict[str, Any]]:
    stack = [question]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.get("parts", [])))


def leaf_questions(question: dict[str, Any]) -> Iterator[dict[str, Any]]:
    return (node for node in walk_questions(question) if node.get("type") != "composite")


def figure_asset_ids(question: dict[str, Any]) -> set[str]:
    return {
        identifier for node in walk_questions(question) for identifier in node.get("figure_asset_ids", [])
    }


def audio_work(question: dict[str, Any]) -> Iterator[tuple[dict[str, Any], dict[str, Any], str]]:
    for node in walk_questions(question):
        for material in node.get("materials", []):
            if material.get("kind") != "listening":
                continue
            text, audio = material.get("text", "").strip(), material.get("audio_asset_id")
            if audio and not text:
                yield node, material, "speech_recognition"
            elif text and (
                not audio
                or (
                    material.get("audio_generated_from")
                    and material["audio_generated_from"]
                    != hashlib.sha256(material["text"].encode()).hexdigest()
                )
            ):
                yield node, material, "speech_synthesis"


def merge_question_input(old: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Preserve server-owned child results while applying the editable tree by stable ID."""
    old_parts = {part["id"]: part for part in old.get("parts", [])}
    merged = {**old, **data}
    merged["parts"] = [
        merge_question_input(old_parts.get(part["id"], {}), part) for part in data.get("parts", [])
    ]
    from .schemas import QuestionInput

    previous = QuestionInput.model_validate(old).model_dump()
    if any(value != previous.get(key) for key, value in data.items() if key not in {"id", "revision"}):
        merged["explanation_stale"] = bool(old.get("explanation")) or old.get("explanation_stale", False)
    return merged


def answer_input(question: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "id",
        "type",
        "stem",
        "options",
        "answer",
        "asset_ids",
        "reference_text",
        "reference_asset_ids",
        "materials",
        "rendered_figures",
        "figure_asset_ids",
    )
    return {
        **{key: question.get(key) for key in fields},
        "parts": [answer_input(part) for part in question.get("parts", [])],
    }
