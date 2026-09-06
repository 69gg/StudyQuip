"""HTTP 业务输入及题型完整性校验。"""

import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Option(BaseModel):
    id: str = Field(min_length=1)
    text: str = ""


class QuestionInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    revision: int | None = None
    subject_id: str = ""
    type: Literal["single_choice", "multiple_choice", "fill_blank", "short_answer"] = "single_choice"
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


def validate_question(question: dict[str, Any], require_confirmed: bool = True) -> None:
    if not question.get("subject_id"):
        raise ValueError("请选择科目")
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
    if require_confirmed and not question.get("answer_confirmed"):
        raise ValueError("请先确认正确答案")


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
    subject_id: str | None = None
    book_ids: list[str] | None = None
    node_id: str | None = None
    mode: Literal["hybrid", "keyword", "phrase", "semantic"] = "hybrid"
    keyword_mode: Literal["any", "all"] = "any"
    limit: int = Field(12, ge=1, le=100)
