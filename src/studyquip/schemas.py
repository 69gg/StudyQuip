"""HTTP 业务输入及题型完整性校验。"""

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .question_tree import FigureSpec, Material, walk_questions


class Option(BaseModel):
    id: str = Field(min_length=1)
    text: str = ""


class QuestionInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    revision: int | None = None
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    subject_id: str = ""
    type: Literal["single_choice", "multiple_choice", "fill_blank", "short_answer", "composite"] = (
        "single_choice"
    )
    stem: str = ""
    options: list[Option] = Field(default_factory=list)
    answer: str | list[str] | None = None
    wrong_answer: str | list[str] | None = ""
    error_reason: str = ""
    optimize_error_reason: bool = False
    notes: str = ""
    reference_text: str = ""
    asset_ids: list[str] = Field(default_factory=list)
    reference_asset_ids: list[str] = Field(default_factory=list)
    figure_asset_ids: list[str] = Field(default_factory=list)
    book_ids: list[str] = Field(default_factory=list)
    parts: list["QuestionInput"] = Field(default_factory=list)
    materials: list[Material] = Field(default_factory=list)
    rendered_figures: list[FigureSpec] = Field(default_factory=list)
    figure_requirements: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_nodes_and_materials(self) -> "QuestionInput":
        nodes: set[str] = set()
        materials: set[str] = set()
        stack = [self]
        while stack:
            node = stack.pop()
            if node.id and node.id in nodes:
                raise ValueError("题目树中的节点 ID 不能重复")
            nodes.add(node.id)
            for material in node.materials:
                if material.id in materials:
                    raise ValueError("题目树中的材料 ID 不能重复")
                materials.add(material.id)
            stack.extend(node.parts)
        return self


def validate_question(question: dict[str, Any], require_confirmed: bool = True) -> None:
    if not question.get("subject_id"):
        raise ValueError("请选择科目")
    seen: set[str] = set()
    for node in walk_questions(question):
        identifier = node.get("id")
        if identifier:
            if identifier in seen:
                raise ValueError("题目树中的节点 ID 不能重复")
            seen.add(identifier)
        for material in node.get("materials", []):
            if material.get("kind") == "listening" and not (
                material.get("text", "").strip() or material.get("audio_asset_id")
            ):
                raise ValueError("听力材料需要音频或文稿，二者也可同时提供")
        if node.get("figure_requirements") and not node.get("figure_asset_ids"):
            raise ValueError("题目需要补充插图，请上传后确认")
        if node.get("type") == "composite":
            if not node.get("parts"):
                raise ValueError("大题至少需要一个小题")
        else:
            if node.get("parts"):
                raise ValueError("包含小题的节点应设置为大题")
            validate_question_leaf(node)
    if require_confirmed and not question.get("answer_confirmed"):
        raise ValueError("请先确认全部题目的正确答案")


def validate_question_leaf(question: dict[str, Any]) -> None:
    if not str(question.get("stem", "")).strip():
        raise ValueError("题干不能为空")
    answer = question.get("answer")
    if answer is None or answer == "" or answer == []:
        raise ValueError("请填写正确答案")
    question_type = question.get("type")
    if question_type in {"single_choice", "multiple_choice"}:
        options = question.get("options", [])
        identifiers = [option["id"] for option in options]
        if (
            len(options) < 2
            or len(set(identifiers)) != len(identifiers)
            or any(not option["text"].strip() for option in options)
        ):
            raise ValueError("选择题至少需要两个非空选项，且选项 ID 不能重复")
        answers = [answer] if isinstance(answer, str) else answer
        if not isinstance(answers, list) or not set(answers).issubset(set(identifiers)):
            raise ValueError("正确答案必须对应选项 ID")
        if question_type == "single_choice" and len(answers) != 1:
            raise ValueError("单选题只能有一个正确选项")
        if len(set(answers)) != len(answers):
            raise ValueError("答案中不能有重复选项")
    elif question_type == "fill_blank":
        if isinstance(answer, str) and not answer.strip():
            raise ValueError("填空题答案不能为空白")
        if isinstance(answer, list) and any(not item.strip() for item in answer):
            raise ValueError("填空题每个答案都不能为空")
    elif question_type == "short_answer":
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("简答题答案应为非空文本")


class BookInput(BaseModel):
    revision: int | None = None
    title: str = Field(min_length=1)
    subject_id: str
    text: str = ""
    asset_ids: list[str] = Field(default_factory=list)
    extra_processing_budget: int | None = Field(default=None, ge=0)


class ScheduleInput(BaseModel):
    not_before: float | None = None
    delay_seconds: float | None = Field(default=None, ge=0)
    bypass_window: bool = False

    @model_validator(mode="after")
    def check_times(self) -> "ScheduleInput":
        if self.not_before is not None and self.delay_seconds is not None:
            raise ValueError("预约时间与相对延迟只能选择一个")
        return self

    def timestamp(self) -> float:
        if self.not_before is not None:
            return self.not_before
        return time.time() + (self.delay_seconds or 0)


class ExportInput(BaseModel):
    question_ids: list[str] = Field(min_length=1)
    mode: Literal["practice", "review"] = "practice"
    include_answer: bool = True
    include_explanation: bool = True
    include_knowledge: bool = True
    blank_lines: int = Field(5, ge=0, le=50)


class SearchInput(BaseModel):
    query: str
    target: Literal["book", "question"] = "book"
    context: Literal["search", "print"] = "search"
    subject_id: str | None = None
    book_ids: list[str] | None = None
    node_id: str | None = None
    methods: list[Literal["keyword", "semantic"]] = Field(
        default_factory=lambda: ["keyword"], min_length=1, max_length=2
    )
    # Single-method clients remain valid; removed combined modes are deliberately rejected.
    mode: Literal["keyword", "phrase", "semantic"] | None = None
    keyword_mode: Literal["any", "all", "phrase"] = "any"
    parts: list[
        Literal[
            "stem", "options", "answer", "explanation", "knowledge", "material", "reference", "error_reason"
        ]
    ] = Field(default_factory=lambda: ["stem"], min_length=1)
    question_types: list[
        Literal["single_choice", "multiple_choice", "fill_blank", "short_answer", "composite"]
    ] = Field(default_factory=list)
    confirmed_only: bool = False
    limit: int | None = Field(None, ge=1)

    @model_validator(mode="after")
    def validate_search(self) -> "SearchInput":
        if self.mode:
            requested = ["keyword" if self.mode == "phrase" else self.mode]
            if "methods" in self.model_fields_set and self.methods != requested:
                raise ValueError("mode 与 methods 冲突")
            self.methods = requested
            if self.mode == "phrase":
                self.keyword_mode = "phrase"
        if len(set(self.methods)) != len(self.methods):
            raise ValueError("检索方式不能重复")
        self.parts = list(dict.fromkeys(self.parts))
        if self.target == "question" and (self.book_ids or self.node_id):
            raise ValueError("题目检索不接受教材目录范围；请使用科目、题型和题目部分筛选")
        return self
