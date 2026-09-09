"""Resumable application jobs. Every business write is fenced by JobStore."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints
from sqlalchemy.engine import Connection

from studyquip.ai import (
    AIProtocolError,
    AIService,
    ModelProfile,
    ToolHandler,
    estimate_tokens,
    tool_definition,
)
from studyquip.db import ConflictError, Database
from studyquip.jobs import JobStore, source_fingerprint
from studyquip.question_tree import FigureSpec, Material, walk_questions

if TYPE_CHECKING:
    from studyquip.schemas import SearchInput

Json = dict[str, Any]


class NeedsReview(RuntimeError):
    pass


class Structured(BaseModel):
    model_config = ConfigDict(extra="forbid")


def decode_model_list(value: Any) -> Any:
    """Decode unambiguous array encodings only; item validation still belongs to Pydantic."""
    if isinstance(value, dict) and set(value) == {"item"}:
        item = value["item"]
        return item if isinstance(item, list) else [item]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        if isinstance(decoded, list):
            return decoded
    return value


ListItem = TypeVar("ListItem")
# An unnamed Annotated alias preserves the existing provider-facing array schema.
ModelList = Annotated[list[ListItem], BeforeValidator(decode_model_list)]


class Option(Structured):
    id: str
    text: str


class QuestionFormattingIssue(Structured):
    field: Literal["stem", "option"]
    option_id: str | None = None
    message: str = Field(min_length=1)


class QuestionDraft(Structured):
    id: str | None = None
    subject_id: str | None = None
    type: Literal["single_choice", "multiple_choice", "fill_blank", "short_answer", "composite"]
    stem: str = ""
    options: ModelList[Option] = Field(default_factory=list)
    answer_from_reference: str | ModelList[str] | None = None
    wrong_answer: str | None = None
    reference_analysis: str | None = None
    # Cached results from before formula formatting must not rewrite existing text.
    formatting_issues: ModelList[QuestionFormattingIssue] | None = None
    parts: ModelList["QuestionDraft"] = Field(default_factory=list)
    materials: ModelList[Material] = Field(default_factory=list)
    rendered_figures: ModelList[FigureSpec] = Field(default_factory=list)
    figure_requirements: ModelList[str] = Field(default_factory=list)
    figure_description: str = ""


class EvidenceDraft(Structured):
    block_id: str
    revision: int
    quote: str
    start: int | None = None
    end: int | None = None


class ExplanationDraft(Structured):
    summary: str
    steps: ModelList[str]
    knowledge_points: ModelList[str]
    citations: ModelList[EvidenceDraft] = Field(default_factory=list)
    answer_conflict: str | None = None
    error_reason_optimized: str | None = None


class OptimizedExplanationDraft(ExplanationDraft):
    error_reason_optimized: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class PageDraft(Structured):
    # Older checkpoints include quality/issues; they no longer control recognition or acceptance.
    model_config = ConfigDict(extra="ignore")
    text: str
    is_blank: bool = False


class BlockDraft(Structured):
    text: str
    node_id: str
    type: str = "paragraph"
    order: float | None = None
    source_page_ids: ModelList[str] = Field(default_factory=list)


class BlockChanges(Structured):
    text: str | None = None
    type: str | None = None
    node_id: str | None = None
    order: float | None = None


class NodeDraft(Structured):
    id: str | None = None
    parent_id: str | None = None
    title: str
    order: float | None = None
    base_revision: int | None = None


class VersionRef(Structured):
    id: str
    revision: int


class InsertOperation(Structured):
    op: Literal["insert"]
    id: str | None = None
    block: BlockDraft


class TextEdit(Structured):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    expected_text: str
    replacement: str


class UpdateOperation(Structured):
    op: Literal["update"]
    id: str
    base_revision: int
    changes: BlockChanges = Field(default_factory=BlockChanges)
    text_edit: TextEdit | None = None


class MoveOperation(Structured):
    op: Literal["move"]
    id: str
    base_revision: int
    node_id: str
    order: float


class SplitOperation(Structured):
    op: Literal["split"]
    id: str
    base_revision: int
    parts: ModelList[str]


class MergeOperation(Structured):
    op: Literal["merge"]
    ids: ModelList[str]
    base_revisions: ModelList[VersionRef]
    text: str | None = None


class ArchiveOperation(Structured):
    op: Literal["archive"]
    id: str
    base_revision: int


class NodeOperation(Structured):
    op: Literal["node"]
    node: NodeDraft


class ConceptDraft(Structured):
    id: str | None = None
    name: str
    aliases: ModelList[str] = Field(default_factory=list)
    evidence: ModelList[EvidenceDraft] = Field(default_factory=list)


class RelationDraft(Structured):
    source_id: str
    target_id: str
    type: str
    evidence: EvidenceDraft


def operation_schema(schema: Json) -> None:
    # Literal op values are disjoint. Keep the existing provider-facing anyOf schema
    # while Pydantic selects one branch locally for useful validation feedback.
    schema.pop("discriminator", None)
    schema["anyOf"] = schema.pop("oneOf")


class RevisionDraft(Structured):
    operations: ModelList[
        Annotated[
            InsertOperation
            | UpdateOperation
            | MoveOperation
            | SplitOperation
            | MergeOperation
            | ArchiveOperation
            | NodeOperation,
            Field(discriminator="op", json_schema_extra=operation_schema),
        ]
    ]
    reason: str
    working_summary: str
    current_node_id: str | None = None
    open_anchors: ModelList[str] = Field(default_factory=list)
    concepts: ModelList[ConceptDraft] = Field(default_factory=list)
    relations: ModelList[RelationDraft] = Field(default_factory=list)
    closed_node_ids: ModelList[str] = Field(default_factory=list)


class SummaryItem(Structured):
    node_id: str
    summary: str


class Summaries(Structured):
    summaries: ModelList[SummaryItem]


class Probe(Structured):
    ok: bool


class PipelineContext:
    def __init__(self, db: Database, jobs: JobStore, ai: AIService, settings: Any, job: Json) -> None:
        self.db, self.jobs, self.ai, self.settings, self.job = db, jobs, ai, settings, job
        self.data: Json = copy.deepcopy(job.get("checkpoint") or {})
        self._save_lock = asyncio.Lock()
        self.resource_kind: str | None = None
        self.parent_book_id: str | None = (job.get("payload") or {}).get("book_id")
        self.parent_source_kind: str | None = {
            "page_recognize": "page",
            "suggestion_regenerate": "suggestion",
        }.get(job["kind"])
        if job["kind"] in {"book_process", "book_index"}:
            self.parent_book_id = job["resource_id"]

    @property
    def lease(self) -> tuple[str, str, str]:
        return self.job["id"], self.job["owner"], self.job["lease_token"]

    async def bind(self, kind: str) -> Json:
        record = await asyncio.to_thread(self.db.get, kind, self.job["resource_id"])
        if record is None or record.get("deleted"):
            raise ValueError("任务来源已删除")
        self.resource_kind = kind
        fingerprint = source_fingerprint(record, kind)
        if "source_fingerprint" not in self.data:
            await self.commit({"source_fingerprint": fingerprint, "source_revision": record["revision"]})
        elif self.data["source_fingerprint"] != fingerprint:
            raise ConflictError("任务输入已修改；请为当前版本重新发起处理")
        return record

    def check_resource(self, conn: Connection | None = None) -> None:
        if self.parent_source_kind:
            source = self.db.get(self.parent_source_kind, self.job["resource_id"], conn=conn)
            if not source or source.get("deleted") or not source.get("book_id"):
                raise ConflictError("教材子任务来源已删除，不能继续处理")
            if self.parent_book_id and self.parent_book_id != source["book_id"]:
                raise ConflictError("教材子任务归属已变化，不能继续处理")
            self.parent_book_id = source["book_id"]
        if self.parent_book_id:
            parent = self.db.get("book", self.parent_book_id, conn=conn)
            if not parent or parent.get("deleted"):
                raise ConflictError("教材已删除，子任务不能继续请求模型或提交结果")
        if self.resource_kind and "source_fingerprint" in self.data:
            record = self.db.get(self.resource_kind, self.job["resource_id"], conn=conn)
            if (
                record is None
                or record.get("deleted")
                or source_fingerprint(record, self.resource_kind) != self.data["source_fingerprint"]
            ):
                raise ConflictError("任务输入已修改，旧结果不能提交")

    async def guard(self) -> None:
        def check() -> None:
            with self.db.read() as conn:
                self.jobs.assert_lease(conn, *self.lease)
                self.check_resource(conn)

        await asyncio.to_thread(check)

    async def commit(
        self,
        patch: Json | None = None,
        mutate: Callable[[Connection], Any] | None = None,
        checkpoint_from_result: Callable[[Any], Json] | None = None,
    ) -> Any:
        async with self._save_lock:
            next_data = {**self.data, **(patch or {}), "last_activity_at": time.time()}
            result: list[Any] = []

            def fenced(conn: Connection) -> None:
                self.check_resource(conn)
                if mutate:
                    result.append(mutate(conn))
                    if checkpoint_from_result:
                        next_data.update(checkpoint_from_result(result[-1]))

            await asyncio.to_thread(self.jobs.checkpoint, *self.lease, next_data, fenced)
            self.data = next_data
            return result[0] if result else None

    async def structured(
        self,
        key: str,
        profile: ModelProfile,
        prompt: str,
        schema: type[BaseModel],
        images: list[str] | None = None,
        tools: dict[str, tuple[str, Json, ToolHandler]] | None = None,
        extra_budget: int | None = None,
    ) -> Any:
        state = copy.deepcopy(self.data.get("stages", {}).get(key, {}))

        async def save(value: Json) -> None:
            async with self._save_lock:
                stages = {**self.data.get("stages", {}), key: copy.deepcopy(value)}
                patch = {**self.data, "stages": stages, "last_activity_at": time.time()}
                if key.startswith("summary:") or key.startswith("summary-collapse:"):
                    old_requests = (
                        self.data.get("stages", {}).get(key, {}).get("usage", {}).get("requests", 0)
                    )
                    new_requests = value.get("usage", {}).get("requests", 0)
                    patch["extra_requests"] = self.data.get("extra_requests", 0) + new_requests - old_requests
                await asyncio.to_thread(self.jobs.checkpoint, *self.lease, patch)
                self.data = patch

        async def before() -> None:
            await self.guard()
            if extra_budget is not None and self.data.get("extra_requests", 0) >= extra_budget:
                raise NeedsReview("教材额外概述预算已用完；增加预算后继续，已完成正文和关键词索引仍可使用")

        return await self.ai.structured(
            profile,
            prompt,
            schema,
            images=images,
            tools=tools,
            state=state,
            save=save,
            before_request=before,
            bypass_window=bool(self.job.get("bypass_window")),
        )

    async def finish(self, result: Json, mutate: Callable[[Connection], Any] | None = None) -> None:
        def fenced(conn: Connection) -> None:
            self.check_resource(conn)
            if mutate:
                mutate(conn)

        await asyncio.to_thread(self.jobs.finish, *self.lease, result, fenced)

    async def assets(self, ids: list[str]) -> list[str]:
        from studyquip.media import image_data_url

        urls: list[str] = []
        for asset_id in ids:
            asset = await asyncio.to_thread(self.db.get, "asset", asset_id)
            if asset is None:
                raise ValueError("图片附件已删除")
            urls.append(await asyncio.to_thread(image_data_url, self.settings, asset))
        return urls


def question_text_fields(question: Json, draft: QuestionDraft) -> Json:
    stem = question.get("stem") or ""
    options = copy.deepcopy(question.get("options") or [])
    warnings: list[str] = []
    issues = draft.formatting_issues or []
    option_ids = {option["id"] for option in options}
    for issue in issues:
        label = "题干"
        if issue.field == "option":
            index = next((i for i, option in enumerate(options) if option["id"] == issue.option_id), None)
            label = f"第 {index + 1} 个选项" if index is not None else "选项"
        warnings.append(f"{label}：{issue.message}")

    if not stem:
        stem = draft.stem
    elif draft.formatting_issues is not None and not any(issue.field == "stem" for issue in issues):
        if draft.stem.strip():
            stem = draft.stem
        else:
            warnings.append("AI 未返回有效题干，已保留原文。")

    if not options or (not question.get("answer") and not any(option["text"].strip() for option in options)):
        options = [option.model_dump() for option in draft.options]
    elif draft.formatting_issues is not None:
        candidates = {option.id: option.text for option in draft.options}
        if set(candidates) != option_ids or len(candidates) != len(draft.options):
            warnings.append("AI 返回的选项 ID 不完整或重复，已保留原选项。")
        else:
            for index, option in enumerate(options):
                blocked = any(
                    issue.field == "option"
                    and (issue.option_id == option["id"] or issue.option_id not in option_ids)
                    for issue in issues
                )
                if blocked:
                    continue
                text = candidates[option["id"]]
                if text.strip():
                    option["text"] = text
                elif option["text"]:
                    warnings.append(f"AI 未返回第 {index + 1} 个选项的有效内容，已保留原文。")
    return {"stem": stem, "options": options, "formatting_warnings": warnings}


def merge_question_draft(question: Json, draft: QuestionDraft, has_reference: bool) -> Json:
    updated = {**question, **question_text_fields(question, draft)}
    for field in ("subject_id", "type", "wrong_answer"):
        value = getattr(draft, field)
        if not updated.get(field) and value is not None:
            updated[field] = value
    if has_reference:
        updated["reference_analysis"] = draft.reference_analysis
        if (
            not question.get("answer_confirmed")
            and not question.get("answer")
            and draft.answer_from_reference is not None
        ):
            updated.update(answer=draft.answer_from_reference, answer_source="reference")
    if not question.get("rendered_figures"):
        updated["rendered_figures"] = [
            {**figure.model_dump(), "id": str(uuid.uuid4())} for figure in draft.rendered_figures
        ]
    updated["figure_requirements"] = draft.figure_requirements
    updated["figure_description"] = draft.figure_description
    if not question.get("materials"):
        updated["materials"] = [
            {**material.model_dump(), "id": str(uuid.uuid4())} for material in draft.materials
        ]
    existing = question.get("parts", [])
    if existing:
        candidates = {part.id: part for part in draft.parts}
        if set(candidates) != {part["id"] for part in existing} or len(candidates) != len(draft.parts):
            updated["formatting_warnings"].append("AI 返回的小题 ID 不完整或重复，已保留原小题结构。")
        else:
            updated["parts"] = [
                merge_question_draft(
                    part,
                    candidates[part["id"]],
                    has_reference or bool(part.get("reference_text") or part.get("reference_asset_ids")),
                )
                for part in existing
            ]
    elif draft.parts and question.get("type") == "composite":
        updated["parts"] = [
            merge_question_draft(
                {
                    "id": str(uuid.uuid4()),
                    "type": part.type,
                    "stem": "",
                    "options": [],
                    "answer": None,
                },
                part,
                has_reference,
            )
            for part in draft.parts
        ]
    if updated != question:
        updated.update(answer_confirmed=False, explanation_stale=bool(question.get("explanation")))
    return updated


async def question_extract(ctx: PipelineContext) -> None:
    question = await ctx.bind("question")
    has_reference = bool(question.get("reference_text") or question.get("reference_asset_ids"))
    ids = list(
        dict.fromkeys(
            identifier
            for node in walk_questions(question)
            for key in ("asset_ids", "figure_asset_ids", "reference_asset_ids")
            for identifier in node.get(key, [])
        )
    )
    profile = await ctx.ai.profile_for("question_vision" if ids else "question_text")
    subjects = await asyncio.to_thread(ctx.db.list, "subject")
    prompt = (
        "整理错题：可以在不改变内容和题意的前提下优化题干、选项的表述，使语言清楚通顺，并规范公式排版。不得解题后改题、纠错、补条件、暗示答案或改变数学含义；保留全部数值、变量、单位、正负号、否定词、条件、选项 ID 与顺序。原表述有歧义时保留并提示核对。已有选项按原 ID 返回全部选项，不新增删除；仅当全部选项为空且尚无答案时可按原图重建。大题 type=composite，parts 可继续嵌套大题，保留已有小题 ID、顺序和层级，不丢弃材料。其余非空字段必须尊重；标准答案只能从参考解析提取，不得用自行推导的答案填 answer_from_reference。不得将图片批注误当题干。\n"
        "你可以按需使用独立的关键词或向量检索和目录工具理解教材术语与图示，但检索不能成为修改题目条件或编造答案的依据。已有材料足够时直接提交。配图优先返回 rendered_figures：几何/实验示意图用仅含静态元素的安全 SVG；函数与坐标图用 plot 的范围、series.expression（仅 x 和常见数学函数）或 points。不能可靠复刻的复杂插图在 figure_requirements 中说明，请用户上传，不猜画。figure_description 忠实描述已有插图中的标签、数值和关系，供文本讲解模型理解，不额外解题或猜测不清晰的信息。材料 materials 的 kind 为 text 或 listening，保留用户音频 ID 和文稿；不得虚构音频附件。\n"
        r"在 stem 和 options.text 中使用 Markdown 与 KaTeX 支持的 LaTeX：行内公式用 $...$，独立公式用单独成行的 $$...$$。将含义明确的分数、根号、上下标、向量和化学式规范排版，例如 x^2/2 写为 $\frac{x^2}{2}$，sqrt(x) 写为 $\sqrt{x}$，H2SO4 写为 $\ce{H2SO4}$。化学反应使用 \ce{...}，保留原系数和反应条件，不自行配平。中文叙述放在公式外，不将全文包入公式或代码块；已正确的 LaTeX 不重复包裹。"
        "\n公式含义不明确（例如未注明分母范围的 1/2x）或图片符号不清时，不猜测：已有字段完整保留原文，空字段忠实转录可辨认内容，并在 formatting_issues 中指出位置与问题（field 为 stem 或 option，选项使用原 option_id）。formatting_issues 必须返回列表，无问题返回 []。\n"
        "做错原因 error_reason 和备注 notes 均由用户填写，题目识别不得生成或改写这两个字段。\n"
        + json.dumps(
            {
                "question": question,
                "subjects": subjects,
                "question_image_count": len(question.get("asset_ids", [])),
                "has_reference": has_reference,
                "image_manifest": [
                    {
                        "position": index + 1,
                        "asset_id": identifier,
                        "uses": [
                            {"node_id": node.get("id"), "field": field}
                            for node in walk_questions(question)
                            for field in ("asset_ids", "figure_asset_ids", "reference_asset_ids")
                            if identifier in node.get(field, [])
                        ],
                    }
                    for index, identifier in enumerate(ids)
                ],
            },
            ensure_ascii=False,
        )
    )
    from studyquip.retrieval import RetrievalService

    with ctx.db.read() as conn:
        books = RetrievalService(ctx.db).scoped_books(
            question.get("book_ids") or None, question.get("subject_id"), conn
        )
    draft: QuestionDraft = await ctx.structured(
        "question_extract",
        profile,
        prompt,
        QuestionDraft,
        await ctx.assets(ids),
        tools=retrieval_tools(ctx, books, question.get("subject_id"), context_tokens=profile.context_tokens),
    )

    def write(conn: Connection) -> None:
        updated = merge_question_draft(question, draft, has_reference)
        if updated.get("subject_id") and not ctx.db.get("subject", updated["subject_id"], conn=conn):
            raise ValueError("模型选择了不存在的科目")
        audio_ids = {
            m.get("audio_asset_id") for n in walk_questions(question) for m in n.get("materials", [])
        }
        for node in walk_questions(updated):
            for material in node.get("materials", []):
                if material.get("audio_asset_id") and material["audio_asset_id"] not in audio_ids:
                    raise ValueError("模型返回了不属于该题目的音频附件")
        updated.update(answer_confirmed=False, status="draft")
        ctx.db.put("question", updated, id=question["id"], expected_revision=question["revision"], conn=conn)

    await ctx.finish({"question_id": question["id"], "requires_answer_confirmation": True}, write)


def retrieval_tools(
    ctx: PipelineContext,
    book_ids: list[str],
    subject_id: str | None = None,
    *,
    context_tokens: int | None = None,
) -> dict[str, tuple[str, Json, ToolHandler]]:
    from studyquip.retrieval import records

    async def search(args: Json) -> Any:
        from studyquip.retrieval import search_limit
        from studyquip.schemas import SearchInput

        request = SearchInput.model_validate(
            {**args, "target": "book", "book_ids": book_ids, "subject_id": subject_id}
        )
        search_limit(ctx.db, request.limit, tool=True)
        # An empty authorized book scope must never expand to all books.
        if not book_ids:
            return []
        return await execute_search(ctx, request)

    async def browse(args: Json) -> Any:
        kind = args.get("kind", "node")
        if kind not in {"book", "node", "concept", "relation"}:
            raise ValueError("不支持的浏览类型")
        rows = await asyncio.to_thread(records, ctx.db, kind)
        rows = [row for row in rows if (row["id"] if kind == "book" else row.get("book_id")) in book_ids]
        if args.get("book_id"):
            if args["book_id"] not in book_ids:
                raise ValueError("书本不在允许范围内")
            rows = [row for row in rows if row.get("book_id", row["id"]) == args["book_id"]]
        if kind == "node" and args.get("node_id"):
            node_map = {row["id"]: row for row in rows}
            target = node_map.get(args["node_id"])
            if target is None:
                raise ValueError("目录不在允许范围内")
            relation = args.get("relation", "children")
            if relation == "ancestors":
                selected: list[Json] = []
                while target:
                    selected.append(target)
                    target = node_map.get(target.get("parent_id"))
                rows = list(reversed(selected))
            else:
                selected = []
                frontier = [(target, 0)]
                maximum = (
                    int(args["depth"])
                    if args.get("depth") is not None
                    else (1 if relation == "children" else None)
                )
                while frontier:
                    current, depth = frontier.pop(0)
                    selected.append(current)
                    if maximum is None or depth < maximum:
                        frontier.extend(
                            (row, depth + 1) for row in rows if row.get("parent_id") == current["id"]
                        )
                rows = selected
        query = str(args.get("query", "")).casefold()
        if query:
            rows = [row for row in rows if query in json.dumps(row, ensure_ascii=False).casefold()]
        offset, limit = max(0, int(args.get("offset", 0))), min(50, max(1, int(args.get("limit", 20))))
        safe = [
            {key: value for key, value in row.items() if key not in {"text", "asset_ids"}} for row in rows
        ]
        return {
            "items": safe[offset : offset + limit],
            "total": len(safe),
            "next_offset": offset + limit if len(safe) > offset + limit else None,
        }

    async def read(args: Json) -> Any:
        block = await asyncio.to_thread(ctx.db.get, "block", str(args["block_id"]))
        if block is None or block.get("book_id") not in book_ids or block.get("archived"):
            raise ValueError(
                "块不存在或超出允许范围；block_id 必须是已提供或检索返回的正文块 ID，"
                "不能使用教材、目录或原页 ID。尚未插入的新正文无法通过 read_block 读取。"
            )
        start = max(0, int(args.get("start") or 0))
        end = int(args.get("end") or len(block["text"]))
        if end < start or end > len(block["text"]):
            raise ValueError("正文范围无效")
        if context_tokens is not None and estimate_tokens(block["text"][start:end]) > context_tokens:
            raise ValueError("正文范围过长，请指定更小的 Unicode 码点区间")
        result = {**block, "text": block["text"][start:end], "range_start": start, "range_end": end}
        if args.get("neighbors"):
            blocks = await asyncio.to_thread(records, ctx.db, "block", {"book_id": block["book_id"]})
            ordered = sorted(
                (row for row in blocks if not row.get("archived")),
                key=lambda row: (row.get("order", 0), row["id"]),
            )
            index = next(index for index, row in enumerate(ordered) if row["id"] == block["id"])
            result["neighbors"] = [
                {"id": row["id"], "revision": row["revision"], "node_id": row.get("node_id")}
                for row in ordered[max(0, index - 1) : index + 2]
                if row["id"] != block["id"]
            ]
        return result

    string = {"type": "string"}
    integer = {"type": "integer"}
    return {
        "search_textbook": (
            "在允许的教材范围内执行独立的关键词或向量检索。默认仅关键词；methods 可指定一种或按先后顺序指定两种，后者只按顺序追加去重，不混合打分。关键词支持 any/all/phrase；limit 是每种方式输出条数上限。可限定目录子树。",
            {
                "type": "object",
                "properties": {
                    "query": string,
                    "methods": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["keyword", "semantic"]},
                        "minItems": 1,
                        "maxItems": 2,
                    },
                    "keyword_mode": {"enum": ["any", "all", "phrase"], "type": "string"},
                    "node_id": {"type": ["string", "null"]},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": min(ctx.settings.search_max_limit, ctx.settings.tool_search_max_limit),
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            search,
        ),
        "browse_textbook": (
            "查询书本、任意目录层级、祖先或子树、概念名称别名及关系；支持分页。",
            {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["book", "node", "concept", "relation"]},
                    "book_id": {"type": ["string", "null"]},
                    "node_id": {"type": ["string", "null"]},
                    "relation": {"type": "string", "enum": ["children", "ancestors", "subtree"]},
                    "depth": integer,
                    "query": string,
                    "offset": integer,
                    "limit": integer,
                },
                "required": ["kind"],
                "additionalProperties": False,
            },
            browse,
        ),
        "read_block": (
            "读取当前原文和版本，必要时指定 Unicode 码点区间；可查询相邻块 ID。",
            {
                "type": "object",
                "properties": {
                    "block_id": string,
                    "start": integer,
                    "end": integer,
                    "neighbors": {"type": "boolean"},
                },
                "required": ["block_id"],
                "additionalProperties": False,
            },
            read,
        ),
    }


async def question_explain(ctx: PipelineContext) -> None:
    from studyquip.retrieval import RetrievalService, node_path
    from studyquip.schemas import validate_question
    from studyquip.textbook import TextbookService

    question = await ctx.bind("question")
    validate_question(question, require_confirmed=True)
    if any(
        material.get("kind") == "listening" and not material.get("text", "").strip()
        for node in walk_questions(question)
        for material in node.get("materials", [])
    ):
        raise NeedsReview("听力录音尚无文稿；请配置语音识别模型并补全文稿，或手动填写后生成讲解")
    profile = await ctx.ai.profile_for("question_text")
    with ctx.db.read() as conn:
        book_ids = RetrievalService(ctx.db).scoped_books(
            question.get("book_ids") or None, question.get("subject_id"), conn
        )
    tools = retrieval_tools(ctx, book_ids, question.get("subject_id"), context_tokens=profile.context_tokens)
    targets: list[tuple[Json, list[Json]]] = []
    stack: list[tuple[Json, list[Json]]] = [(question, [])]
    while stack:
        node, ancestors = stack.pop()
        if node.get("type") != "composite" or (
            node.get("optimize_error_reason") and node.get("error_reason", "").strip()
        ):
            targets.append((node, ancestors))
        context = {key: node.get(key) for key in ("id", "stem", "materials", "reference_text", "notes")}
        stack.extend((part, [*ancestors, context]) for part in reversed(node.get("parts", [])))

    async def explain(node: Json, ancestors: list[Json]) -> tuple[str, ExplanationDraft, bool]:
        optimize = bool(node.get("optimize_error_reason") and node.get("error_reason", "").strip())
        initial = (
            await tools["search_textbook"][2]({"query": node.get("stem", ""), "methods": ["keyword"]})
            if book_ids
            else []
        )
        prompt = (
            "为这道已确认答案的错题生成讲解、分步分析和知识点。结合祖先大题的题干和材料理解当前小题；"
            "标准答案由用户确认，不得替换，冲突写入 answer_conflict。可自行调用独立的关键词或向量检索、目录及正文工具寻找依据。"
            "优先使用参考解析和教材证据，引用只显示书名及实际目录路径，不显示教材页码。"
            "没有命中原文时 citations 留空；引文必须包含原文、块 ID 和当前 revision。不要臆测无法看到的插图。\n"
            "error_reason 是用户自述的做错原因，原文和备注不可改写。仅当 error_reason_optimization_enabled "
            "为 true 时，在 error_reason_optimized 中优化其用词与条理，保留原意、第一人称和不确定程度；"
            "不得从题目、答案或教材推断、补充用户没有写出的错因。为 false 时该字段必须为 null。\n"
            + json.dumps(
                {
                    "question": node,
                    "ancestor_materials": ancestors,
                    "initial_evidence": initial,
                    "allowed_book_ids": book_ids,
                    "error_reason_optimization_enabled": optimize,
                },
                ensure_ascii=False,
            )
        )
        key = "question_explain" if node["id"] == question["id"] else f"question_explain:{node['id']}"
        draft = await ctx.structured(
            key, profile, prompt, OptimizedExplanationDraft if optimize else ExplanationDraft, tools=tools
        )
        return node["id"], draft, optimize

    # The request scheduler applies the configured model/credential limits to independent leaves.
    outputs = await asyncio.gather(
        *(explain(node, ancestors) for node, ancestors in targets), return_exceptions=True
    )
    results: list[tuple[str, ExplanationDraft, bool]] = []
    for output in outputs:
        if isinstance(output, BaseException):
            raise output
        results.append(output)
    service = TextbookService(ctx.db)

    def write(conn: Connection) -> None:
        updated = copy.deepcopy(question)
        nodes = {node["id"]: node for node in walk_questions(updated)}
        for identifier, draft, optimize in results:
            citations: list[Json] = []
            for citation in draft.citations:
                block = ctx.db.get("block", citation.block_id, conn=conn)
                if not block or block.get("book_id") not in book_ids:
                    raise ValueError("讲解引文超出所选教材范围")
                hint = (
                    (citation.start, citation.end)
                    if citation.start is not None and citation.end is not None
                    else None
                )
                evidence = service.current_evidence(
                    block["book_id"], citation.block_id, citation.revision, citation.quote, hint, conn=conn
                )
                book = ctx.db.get("book", block["book_id"], conn=conn) or {}
                citations.append(
                    {
                        **evidence,
                        "book_id": block["book_id"],
                        "book_title": book.get("title", ""),
                        "node_path": node_path(ctx.db, block.get("node_id"), conn=conn),
                    }
                )
            explanation = {
                **draft.model_dump(),
                "citations": citations,
                "has_textbook_evidence": bool(citations),
            }
            if not optimize:
                explanation["error_reason_optimized"] = None
            nodes[identifier].update(explanation=explanation, explanation_stale=False)
        updated.update(explanation_stale=False, status="ready")
        ctx.db.put("question", updated, id=question["id"], expected_revision=question["revision"], conn=conn)

    await ctx.finish({"question_id": question["id"], "explained_parts": len(results)}, write)


async def question_audio(ctx: PipelineContext) -> None:
    from studyquip.media import safe_path, store_upload
    from studyquip.question_tree import audio_work

    question = await ctx.bind("question")
    completed = copy.deepcopy(ctx.data.get("audio_results", {}))
    missing: list[str] = []
    profiles = await ctx.ai.profiles()
    for node, material, role in audio_work(question):
        key = material["id"]
        if key in completed:
            continue
        if not any(profile.role == role for profile in profiles):
            missing.append(role)
            continue
        profile = await ctx.ai.profile_for(role)
        await ctx.commit(
            {
                "phase": "补全文稿" if role == "speech_recognition" else "生成听力音频",
                "current_material": material.get("title") or "听力材料",
            }
        )

        async def activity(value: Json) -> None:
            await ctx.commit(
                {
                    "audio_activity": {
                        **value,
                        "model": profile.model,
                        "role": profile.role,
                        "profile_id": profile.id,
                        "revision": profile.revision,
                    }
                }
            )

        file: tuple[str, bytes, str] | None = None
        if role == "speech_recognition":
            asset = await asyncio.to_thread(ctx.db.get, "asset", material["audio_asset_id"])
            if not asset or not asset["mime"].startswith("audio/"):
                raise ValueError("听力音频已丢失或格式不正确")
            content = await asyncio.to_thread(safe_path(ctx.settings, asset["path"]).read_bytes)
            file = (asset["name"], content, asset["mime"])
        response = await ctx.ai.audio(
            profile,
            text=material.get("text", ""),
            file=file,
            before_request=ctx.guard,
            bypass_window=bool(ctx.job.get("bypass_window")),
            activity=activity,
        )
        result: Json = {"node_id": node["id"], "usage": response.get("usage", {})}
        generated: Json | None = None
        if role == "speech_recognition":
            text = response.get("text", "").strip()
            if not text:
                raise AIProtocolError("语音识别返回了空文稿；原音频已保留，可继续重试")
            result["text"] = text
        else:
            generated = await asyncio.to_thread(
                store_upload, ctx.settings, "听力-合成.mp3", response["audio"]
            )
            result.update(
                audio_asset_id=generated["id"],
                audio_generated_from=hashlib.sha256(material["text"].encode()).hexdigest(),
            )
        completed[key] = result

        def publish(conn: Connection) -> None:
            if generated:
                ctx.db.put("asset", generated, id=generated["id"], conn=conn)

        await ctx.commit({"audio_results": completed, "audio_activity": None}, publish)

    def write(conn: Connection) -> None:
        updated = copy.deepcopy(question)
        for node in walk_questions(updated):
            for material in node.get("materials", []):
                result = completed.get(material["id"], {})
                material.update(
                    {key: value for key, value in result.items() if key not in {"node_id", "usage"}}
                )
        if updated != question:
            for node in walk_questions(updated):
                if node.get("explanation"):
                    node["explanation_stale"] = True
            ctx.db.put(
                "question", updated, id=question["id"], expected_revision=question["revision"], conn=conn
            )

    await ctx.finish(
        {
            "question_id": question["id"],
            "completed_materials": len(completed),
            "missing_model_roles": list(dict.fromkeys(missing)),
        },
        write,
    )


async def _prepare_pages(ctx: PipelineContext, book: Json) -> list[Json]:
    from studyquip.media import prepare_book_pages
    from studyquip.retrieval import records
    from studyquip.textbook import TextbookService

    existing = await asyncio.to_thread(records, ctx.db, "page", {"book_id": book["id"]})
    imported = {page.get("source_asset_id") for page in existing}
    prepared: list[Json] = []
    text_id = "text:" + hashlib.sha256(book.get("text", "").encode()).hexdigest()
    if book.get("text") and text_id not in imported:
        prepared.append(
            {
                "source_asset_id": text_id,
                "page_index": 0,
                "text": book["text"],
                "source_type": "text",
                "status": "draft",
            }
        )
    for asset_id in book.get("asset_ids", []):
        if asset_id in imported:
            continue
        asset = await asyncio.to_thread(ctx.db.get, "asset", asset_id)
        if not asset:
            raise ValueError("教材原件已删除")
        prepared.extend(await asyncio.to_thread(prepare_book_pages, ctx.settings, asset))
    offset = max((page.get("index", 0) for page in existing), default=-1) + 1

    def save(conn: Connection) -> list[Json]:
        TextbookService(ctx.db).ensure_root(book["id"], conn=conn)
        result: list[Json] = []
        for index, page in enumerate(prepared):
            image_asset = page.get("image_asset")
            if image_asset:
                ctx.db.put("asset", image_asset, id=image_asset["id"], conn=conn)
            page_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"studyquip:{book['id']}:{page['source_asset_id']}:{page.get('page_index', 0)}",
                )
            )
            result.append(
                ctx.db.put(
                    "page",
                    {
                        **page,
                        "book_id": book["id"],
                        "index": offset + index,
                        "status": page.get("status", "pending"),
                        "original_text": page.get("text", ""),
                    },
                    id=page_id,
                    conn=conn,
                )
            )
        return result

    if prepared or not existing:
        added = await ctx.commit({"phase": "识别教材草稿"}, save)
        existing.extend(added)
    original_order = sorted(existing, key=lambda page: (page.get("index", 0), page["id"]))
    source_order = {value: index for index, value in enumerate([text_id, *book.get("asset_ids", [])])}
    ordered = sorted(
        existing,
        key=lambda page: (
            source_order.get(page.get("source_asset_id"), len(source_order)),
            page.get("page_index", 0),
            page["id"],
        ),
    )

    def neighbors(pages: list[Json]) -> dict[str, tuple[str | None, str | None]]:
        return {
            page["id"]: (
                pages[index - 1]["id"] if index else None,
                pages[index + 1]["id"] if index + 1 < len(pages) else None,
            )
            for index, page in enumerate(pages)
        }

    previous, current = neighbors(original_order), neighbors(ordered)
    changed = [
        page
        for index, page in enumerate(ordered)
        if page.get("index") != index or previous[page["id"]] != current[page["id"]]
    ]
    if changed:

        def reorder(conn: Connection) -> list[Json]:
            result: list[Json] = []
            for index, page in enumerate(ordered):
                if page in changed:
                    data = {**page, "index": index}
                    if page.get("status") == "processed":
                        data.update(
                            {
                                "status": "needs_review",
                                "order_changed": True,
                                "issues": [
                                    *page.get("issues", []),
                                    "原件顺序或相邻页面已改变，请校对续接边界后重新处理。",
                                ],
                            }
                        )
                    page = ctx.db.put(
                        "page", data, id=page["id"], expected_revision=page["revision"], conn=conn
                    )
                result.append(page)
            return result

        ordered = await ctx.commit(mutate=reorder)
    return ordered


async def _page_images(ctx: PipelineContext, page: Json) -> list[str]:
    from studyquip.media import image_data_url

    asset = page.get("image_asset")
    if not asset and page.get("source_asset_id"):
        asset = await asyncio.to_thread(ctx.db.get, "asset", page["source_asset_id"])
    if not asset:
        return []
    return [await asyncio.to_thread(image_data_url, ctx.settings, asset)]


async def recognize_page(ctx: PipelineContext, page: Json, force: bool = False) -> Json:
    if page.get("status") == "needs_review" and not force:
        from studyquip.textbook import TextbookService

        return await ctx.commit(
            mutate=lambda conn: TextbookService(ctx.db).restore_review_page(page["book_id"], page["id"], conn)
        )
    if page.get("status") in {"draft", "processed", "skipped"} and not force:
        return page
    original_text = page.get("original_text", page.get("text", ""))
    if page.get("source_type") == "text":
        result = PageDraft(text=original_text)
    else:
        profile = await ctx.ai.profile_for("book_vision")
        prompt = (
            "将这一页教材忠实整理成连续文章，保留标题、正文、例题、侧栏、tips 和补充知识。用明确的‘插图描述’说明可见图像，不补造看不到的信息。多栏按阅读顺序整理，目录条目逐条保留。空白页可以 text 为空且 is_blank=true。只提取本页可见内容，页尾句子未结束是正常跨页；保留原文断句，不添加缺失或截断的判断，不猜补后续内容。本轮只做草稿，跨页续接由后续整理处理。\n可提取的原始文字参考："
            + original_text
        )
        result = await ctx.structured(
            f"recognize:{page['id']}:{page['revision']}:0",
            profile,
            prompt,
            PageDraft,
            await _page_images(ctx, page),
        )

    def save(conn: Connection) -> Json:
        current = ctx.db.get("page", page["id"], conn=conn)
        if not current or current["revision"] != page["revision"]:
            raise ConflictError("页面在识别期间已被修改")
        payload = {
            **current,
            "is_blank": result.is_blank,
            "status": "draft",
            "text": result.text,
            "recognition_draft": result.text,
        }
        payload.pop("quality", None)
        payload.pop("issues", None)
        return ctx.db.put("page", payload, id=page["id"], expected_revision=page["revision"], conn=conn)

    return await ctx.commit(mutate=save)


def wire_operations(draft: RevisionDraft, page_id: str) -> list[Json]:
    operations: list[Json] = []
    for operation in draft.operations:
        value = operation.model_dump(exclude_none=True)
        if value["op"] == "merge":
            value["base_revisions"] = {item["id"]: item["revision"] for item in value["base_revisions"]}
        if value["op"] == "insert":
            value["block"]["source_page_ids"] = list(
                dict.fromkeys([*value["block"].get("source_page_ids", []), page_id])
            )
        operations.append(value)
    return operations


async def _revise_page(ctx: PipelineContext, book: Json, page: Json, profile: ModelProfile) -> None:
    from studyquip.context import ContextBuilder
    from studyquip.textbook import TextbookService

    profile = await ctx.ai.refresh_profile(profile)
    tools = retrieval_tools(ctx, [book["id"]], context_tokens=profile.context_tokens)
    definitions = [
        tool_definition("submit_result", "提交教材修订结果", RevisionDraft.model_json_schema(), profile),
        *(
            tool_definition(name, description, schema, profile)
            for name, (description, schema, _) in tools.items()
        ),
    ]
    input_budget = profile.context_tokens
    # Keep room for the surrounding prompt and at least one subsequent read-tool result.
    # This internal reserve does not become a provider output limit when the field is unset.
    output_reserve = (
        profile.max_output_tokens if profile.max_output_tokens is not None else ctx.settings.output_tokens
    )
    page_key = f"{page['id']}:{page['revision']}"
    plan = ctx.data.get("revision_plans", {}).get(page_key, {})
    # A model/settings edit must not move boundaries underneath completed unit indices.
    builder = ContextBuilder(
        ctx.db,
        token_budget=(
            max(512, input_budget - min(output_reserve, input_budget // 4))
            if input_budget is not None
            else None
        ),
    )
    service = TextbookService(ctx.db)
    unit_index = 0
    unit_budget = plan.get("unit_budget")
    if plan and unit_budget is None:
        # A saved null means one whole-page unit, even if a later model has an explicit budget.
        unit_budget = estimate_tokens(page.get("text", ""))
    first_context = await asyncio.to_thread(
        builder.build,
        book["id"],
        page["id"],
        tools=definitions,
        unit_index=0,
        unit_budget=unit_budget,
    )
    unit_budget = first_context["current_page"]["unit_budget"]
    if not plan:
        await ctx.commit(
            {
                "revision_plans": {
                    **ctx.data.get("revision_plans", {}),
                    page_key: {"context_budget": builder.token_budget, "unit_budget": unit_budget},
                }
            }
        )
    unit_count = first_context["current_page"].get("unit_count", 1)
    while True:
        if unit_index >= unit_count:
            break
        key = f"revise:{page['id']}:{page['revision']}:{unit_index}"
        if key in ctx.data.get("completed_units", []):
            unit_index += 1
            continue
        context = (
            first_context
            if unit_index == 0
            else await asyncio.to_thread(
                builder.build,
                book["id"],
                page["id"],
                tools=definitions,
                unit_index=unit_index,
                unit_budget=unit_budget,
            )
        )
        unit_count = context["current_page"].get("unit_count", 1)
        prompt = (
            "current_page.text 就是本次处理单元的草稿，无需再用工具读取当前页。"
            "current_page.id 是原页 ID，只用于 source_page_ids；ancestors/outline 中的 ID 是目录 ID，"
            "两者都不能作为 read_block.block_id。只新增正文且上下文已足够时直接提交，"
            "仅在确实缺少依据或需要修改已有正文时使用读取工具；不要为完成流程反复浏览同一目录。"
            "按原始顺序把当前教材草稿修订为正式文章。目录可以任意深度，保留真实标题，不硬编码章节。使用块级操作，修改前文必须读取最新块及版本；摘要不能代替原文。新目录和新块使用新唯一 ID；现有 ID 来自上下文或工具。当前页续接前页可合并，保留最前块 ID。遇到跳过缺口禁止拼句或补造目录。人工保护块只能形成建议，但当前页新正文必须先独立插入。每个内容块尽量是一段话，插图描述和侧栏为独立块。提交工作摘要和未闭合锚点，不重复整本前文。概念、别名和关系可随本次提取，但必须引用实际块版本与原文；新插入块版本为 1。无证据留空。只在当前单元末尾关闭确实结束的目录节点。\n"
            + json.dumps(context, ensure_ascii=False)
        )
        receipt: Json = {}
        for correction in range(2):
            stage = f"{key}:{correction}"
            draft: RevisionDraft = await ctx.structured(stage, profile, prompt, RevisionDraft, tools=tools)
            group_id = ctx.data.get("operation_ids", {}).get(stage)
            if not group_id:
                group_id = str(uuid.uuid4())
                await ctx.commit({"operation_ids": {**ctx.data.get("operation_ids", {}), stage: group_id}})
            operations = wire_operations(draft, page["id"])

            def apply(conn: Connection) -> Json:
                current = ctx.db.get("page", page["id"], conn=conn)
                if not current or current["revision"] != page["revision"]:
                    raise ConflictError("页面草稿已变化，旧修订不能提交")
                current_node_id = draft.current_node_id or (context.get("ancestors") or [{}])[-1].get(
                    "id", f"root:{book['id']}"
                )
                answer = service.apply_operations(
                    book["id"],
                    group_id,
                    operations,
                    draft.reason,
                    sources=[{"page_id": page["id"], "revision": page["revision"]}],
                    checkpoint={
                        "current_node_id": current_node_id,
                        "summary": draft.working_summary,
                        "open_anchors": draft.open_anchors,
                        "page_id": page["id"],
                    },
                    conn=conn,
                )
                if answer["status"] == "accepted":
                    for concept in draft.concepts:
                        data = concept.model_dump(exclude_none=True)
                        for evidence in data["evidence"]:
                            if evidence.get("start") is not None and evidence.get("end") is not None:
                                evidence["span_hint"] = [evidence.pop("start"), evidence.pop("end")]
                        try:
                            service.record_concept(book["id"], data, conn=conn)
                        except ValueError as error:
                            ctx.db.put(
                                "relation_rejection",
                                {
                                    "book_id": book["id"],
                                    "kind": "concept",
                                    "reason": str(error),
                                    "name": concept.name,
                                },
                                conn=conn,
                            )
                    for relation in draft.relations:
                        data = relation.model_dump(exclude_none=True)
                        data["evidence"] = [data["evidence"]]
                        service.record_relation(book["id"], data, conn=conn)
                return answer

            def progress(answer: Json) -> Json:
                if answer["status"] != "accepted":
                    return {}
                return {
                    "completed_units": [*ctx.data.get("completed_units", []), key],
                    "closed_node_ids": list(
                        dict.fromkeys([*ctx.data.get("closed_node_ids", []), *draft.closed_node_ids])
                    ),
                }

            receipt = await ctx.commit(
                {"phase": f"顺序修订教材：{page.get('index', 0) + 1}"}, apply, progress
            )
            if receipt["status"] == "accepted":
                break
            prompt += "\n前次操作组已耗尽，修正后会使用新的操作组 ID。拒绝原因：" + json.dumps(
                receipt, ensure_ascii=False
            )
        if receipt.get("status") != "accepted":
            raise NeedsReview("教材修订修正后仍不满足版本或结构约束，请在工作台校对")
        if unit_index + 1 >= unit_count:
            break
        unit_index += 1

    def finish_page(conn: Connection) -> None:
        current = ctx.db.get("page", page["id"], conn=conn)
        if not current or current["revision"] != page["revision"]:
            raise ConflictError("页面在完成修订前已变化")
        ctx.db.put(
            "page",
            {**current, "status": "processed"},
            id=page["id"],
            expected_revision=page["revision"],
            conn=conn,
        )

    await ctx.commit(mutate=finish_page)


async def book_process(ctx: PipelineContext) -> None:
    book = await ctx.bind("book")
    await ctx.commit({"phase": "准备教材原页"})
    pages = await _prepare_pages(ctx, book)
    await ctx.commit({"phase": "识别教材草稿"})
    outputs = await asyncio.gather(*(recognize_page(ctx, page) for page in pages), return_exceptions=True)
    for output in outputs:
        if isinstance(output, BaseException):
            raise output
    for page in outputs:
        if page["status"] in {"processed", "skipped"}:
            continue
        await ctx.commit({"phase": "顺序修订教材", "current_page": page.get("index", 0) + 1})
        await _revise_page(ctx, book, page, await ctx.ai.profile_for("book_text"))

    def finish(conn: Connection) -> None:
        current = ctx.db.get("book", book["id"], conn=conn) or book
        ctx.db.put("book", {**current, "status": "indexing"}, id=book["id"], conn=conn)
        ctx.jobs.enqueue(
            "book_index",
            book["id"],
            bypass_window=bool(ctx.job.get("bypass_window")),
            conn=conn,
            predecessor_id=ctx.job["id"],
        )

    await ctx.finish({"book_id": book["id"], "pages": len(pages)}, finish)


async def page_recognize(ctx: PipelineContext) -> None:
    page = await asyncio.to_thread(ctx.db.get, "page", ctx.job["resource_id"])
    if not page:
        raise ValueError("页面不存在")
    await recognize_page(ctx, page, force=True)

    def resume(conn: Connection) -> None:
        ctx.jobs.wake_book(page["book_id"], conn=conn)

    await ctx.finish({"page_id": page["id"]}, resume)


async def model_test(ctx: PipelineContext) -> None:
    profile = await ctx.ai.profile_for(explicit_id=ctx.job["resource_id"])
    if profile.role == "embedding":
        vectors, usage = await ctx.ai.embed(
            profile,
            ["StudyQuip 模型连接测试"],
            before_request=ctx.guard,
            bypass_window=bool(ctx.job.get("bypass_window")),
        )
        result: Json = {"ok": True, "dimensions": len(vectors[0]), "usage": usage}
    elif profile.role in {"speech_recognition", "speech_synthesis"}:
        import io
        import wave

        file: tuple[str, bytes, str] | None = None
        if profile.role == "speech_recognition":
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(b"\0" * 32000)
            file = ("connection-test.wav", buffer.getvalue(), "audio/wav")
        response = await ctx.ai.audio(
            profile,
            file=file,
            text="This is a StudyQuip audio test.",
            before_request=ctx.guard,
            bypass_window=bool(ctx.job.get("bypass_window")),
        )
        result = {
            "ok": True,
            "audio_bytes": len(response.get("audio", b"")),
            "usage": response.get("usage", {}),
        }
    else:
        probe: Probe = await ctx.structured(
            "model_test", profile, "连接与工具调用协议测试：请调用 submit_result，参数 ok 为 true。", Probe
        )
        result = {"ok": probe.ok, "usage": ctx.data.get("stages", {}).get("model_test", {}).get("usage", {})}
    await ctx.finish(result)


async def export_pdf(ctx: PipelineContext) -> None:
    from studyquip.export import render_export

    result = await render_export(ctx.settings, ctx.db, ctx.job["resource_id"], ctx.job["lease_token"])

    def publish(conn: Connection) -> None:
        snapshot = ctx.db.get("export", ctx.job["resource_id"], conn=conn)
        if not snapshot:
            raise ValueError("导出快照已删除")
        ctx.db.put("export", {**snapshot, **result, "status": "completed"}, id=snapshot["id"], conn=conn)

    await ctx.finish(result, publish)


async def _summarize_nodes(ctx: PipelineContext, book: Json, profile: ModelProfile) -> None:
    from studyquip.context import ContextBuilder, node_source_fingerprint
    from studyquip.retrieval import records

    await ctx.commit({"phase": "生成目录概述"})
    profile = await ctx.ai.refresh_profile(profile)
    nodes = await asyncio.to_thread(records, ctx.db, "node", {"book_id": book["id"]})
    by_id = {node["id"]: node for node in nodes}

    def depth(node: Json) -> int:
        result, seen = 0, set()
        while node.get("parent_id") in by_id:
            if node["id"] in seen:
                raise ValueError("目录出现循环")
            seen.add(node["id"])
            node = by_id[node["parent_id"]]
            result += 1
        return result

    budget = book.get("extra_processing_budget")
    batch_size = ctx.settings.summary_batch_size
    input_budget = profile.context_tokens
    builder = ContextBuilder(ctx.db, token_budget=input_budget)
    ordered = sorted(nodes, key=depth, reverse=True)
    pending: list[Json] = []

    async def run_batch(batch: list[Json]) -> None:
        if not batch:
            return
        requests = int(ctx.data.get("extra_requests", 0))
        if budget is not None and requests >= budget:
            raise NeedsReview("教材额外概述预算已用完；已完成正文和关键词索引可继续使用，增加预算后可继续")
        signature = hashlib.sha256(
            json.dumps([(item["node_id"], item["source_fp"], item["unit_id"]) for item in batch]).encode()
        ).hexdigest()
        prompt = (
            "为这些教材目录节点的原文或子节点概述生成简短概述，仅用于检索路由，不当作原文证据。不添加材料没有的概念。返回每个 node_id 的概述。\n"
            + json.dumps(batch, ensure_ascii=False)
        )
        result: Summaries = await ctx.structured(
            f"summary:{signature}", profile, prompt, Summaries, extra_budget=budget
        )
        expected = {item["node_id"] for item in batch}
        if {item.node_id for item in result.summaries} != expected or len(result.summaries) != len(expected):
            raise AIProtocolError("概述返回的节点集合不匹配")
        summaries = {item.node_id: item.summary for item in result.summaries}

        def write(conn: Connection) -> None:
            for item in batch:
                if node_source_fingerprint(ctx.db, item["node_id"], conn=conn) != item["source_fp"]:
                    raise ConflictError("概述来源已修改")
                ctx.db.put(
                    "summary_part",
                    {
                        "book_id": book["id"],
                        "node_id": item["node_id"],
                        "source_fp": item["source_fp"],
                        "unit_index": item["unit_index"],
                        "text": summaries[item["node_id"]],
                    },
                    id=item["unit_id"],
                    conn=conn,
                )

        await ctx.commit({"phase": "生成目录概述"}, write)

    levels: dict[int, list[Json]] = {}
    for original in ordered:
        levels.setdefault(depth(original), []).append(original)
    for level in sorted(levels, reverse=True):
        ready: list[tuple[Json, str, list[str]]] = []
        for original in levels[level]:
            node = await asyncio.to_thread(ctx.db.get, "node", original["id"])
            if not node:
                continue
            fingerprint = await asyncio.to_thread(node_source_fingerprint, ctx.db, node["id"])
            if node.get("summary_source_fp") == fingerprint and not node.get("summary_stale"):
                continue
            blocks = await asyncio.to_thread(
                records, ctx.db, "block", {"book_id": book["id"], "node_id": node["id"]}
            )
            children = await asyncio.to_thread(
                records, ctx.db, "node", {"book_id": book["id"], "parent_id": node["id"]}
            )
            content = "\n\n".join(
                [
                    *(
                        block["text"]
                        for block in sorted(blocks, key=lambda item: item.get("order", 0))
                        if not block.get("archived")
                    ),
                    *(child.get("summary", "") for child in children if not child.get("summary_stale")),
                ]
            )
            units = builder.units(content, budget=max(256, input_budget // 4) if input_budget else None)
            unit_ids = [
                f"{node['id']}:{fingerprint}:{hashlib.sha256(unit.encode()).hexdigest()}" for unit in units
            ]
            ready.append((node, fingerprint, unit_ids))
            for index, (unit, unit_id) in enumerate(zip(units, unit_ids, strict=True)):
                if await asyncio.to_thread(ctx.db.get, "summary_part", unit_id):
                    continue
                item = {
                    "node_id": node["id"],
                    "title": node["title"],
                    "source_fp": fingerprint,
                    "unit_index": index,
                    "unit_id": unit_id,
                    "text": unit,
                }
                if pending and (
                    len(pending) >= batch_size
                    or any(entry["node_id"] == node["id"] for entry in pending)
                    or (input_budget is not None and estimate_tokens([*pending, item]) > input_budget // 2)
                ):
                    await run_batch(pending)
                    pending = []
                pending.append(item)
        await run_batch(pending)
        pending = []
        # Publish one depth at a time so parent fingerprints see completed child summaries.
        for node, fingerprint, unit_ids in ready:
            parts = [await asyncio.to_thread(ctx.db.get, "summary_part", unit_id) for unit_id in unit_ids]
            summary = "\n".join(part["text"] for part in parts if part)
            while input_budget is not None and estimate_tokens(summary) > input_budget // 4:
                condensed: list[str] = []
                for fragment in builder.units(summary, budget=input_budget // 2):
                    signature = hashlib.sha256(fragment.encode()).hexdigest()
                    result: Summaries = await ctx.structured(
                        f"summary-collapse:{node['id']}:{signature}",
                        profile,
                        "将同一目录节点的分段概述压缩为简洁概述。长度至少缩短一半，保留关键知识，不增加事实。\n"
                        + json.dumps({"node_id": node["id"], "parts": fragment}, ensure_ascii=False),
                        Summaries,
                        extra_budget=budget,
                    )
                    if len(result.summaries) != 1 or result.summaries[0].node_id != node["id"]:
                        raise AIProtocolError("合并概述返回了错误节点")
                    condensed.append(result.summaries[0].summary)
                compacted = "\n".join(condensed)
                if estimate_tokens(compacted) >= estimate_tokens(summary):
                    raise NeedsReview("模型概述没有缩短，已停止额外调用；请调整模型或预算后继续")
                summary = compacted

            def publish(conn: Connection) -> None:
                current = ctx.db.get("node", node["id"], conn=conn)
                if not current or node_source_fingerprint(ctx.db, node["id"], conn=conn) != fingerprint:
                    raise ConflictError("概述来源已变化")
                ctx.db.put(
                    "node",
                    {**current, "summary": summary, "summary_source_fp": fingerprint, "summary_stale": False},
                    id=node["id"],
                    conn=conn,
                )

            await ctx.commit(mutate=publish)


async def book_index(ctx: PipelineContext) -> None:
    from studyquip.retrieval import RetrievalService, embedding_fingerprint

    book = await ctx.bind("book")
    retrieval = RetrievalService(ctx.db)
    await ctx.commit(mutate=lambda conn: retrieval.rebuild(book["id"], conn=conn))
    profiles = await ctx.ai.profiles()
    chat = next((profile for profile in profiles if profile.role == "book_text"), None)
    embedding = next((profile for profile in profiles if profile.role == "embedding"), None)
    if chat:
        await _summarize_nodes(ctx, book, chat)
    # Summaries can take hours; select the embedding profile again at the embedding boundary.
    profiles = await ctx.ai.profiles()
    embedding = next((profile for profile in profiles if profile.role == "embedding"), None)
    if embedding:
        input_budget = embedding.context_tokens
        targets = await asyncio.to_thread(retrieval.embedding_targets, book["id"])
        existing = await asyncio.to_thread(retrieval.existing_embeddings, book["id"], embedding.model_dump())
        batches: list[list[Json]] = []
        for target in targets:
            if (target["object_kind"], target["id"], target["revision"]) in existing:
                continue
            marker = f"{target['object_kind']}:{target['id']}:{target['revision']}:{embedding_fingerprint(embedding.model_dump(), embedding.embedding_dimensions or 0)}"
            if marker in ctx.data.get("embedded", []):
                continue
            if (
                not batches
                or len(batches[-1]) >= ctx.settings.summary_batch_size
                or (
                    input_budget is not None
                    and estimate_tokens([item["text"] for item in [*batches[-1], target]]) > input_budget
                )
            ):
                batches.append([])
            batches[-1].append({**target, "marker": marker})
        total = len(targets)
        completed = total - sum(len(batch) for batch in batches)
        await ctx.commit(
            {"phase": "生成教材向量索引", "embedding_total": total, "embedding_completed": completed}
        )
        space = embedding_fingerprint(embedding.model_dump(), embedding.embedding_dimensions or 0)

        async def latest_embedding() -> ModelProfile:
            from studyquip.scheduling import WindowClosed

            latest = await ctx.ai.profile_for("embedding")
            if embedding_fingerprint(latest.model_dump(), latest.embedding_dimensions or 0) != space:
                raise WindowClosed(time.time(), "嵌入空间配置已变化，保留已有结果并按新空间恢复索引")
            return latest

        async def activity(value: Json) -> None:
            await ctx.commit(
                {"embedding_activity": {**value, "model": embedding.model, "revision": embedding.revision}}
            )

        for batch in batches:
            embedding = await latest_embedding()
            vectors, usage = await ctx.ai.embed(
                embedding,
                [item["text"] for item in batch],
                before_request=ctx.guard,
                bypass_window=bool(ctx.job.get("bypass_window")),
                activity=activity,
            )
            fingerprint = embedding_fingerprint(embedding.model_dump(), len(vectors[0]))

            def write(conn: Connection) -> None:
                for target, vector in zip(batch, vectors, strict=True):
                    retrieval.store_embedding(
                        target["id"],
                        book["id"],
                        target["revision"],
                        fingerprint,
                        vector,
                        target["object_kind"],
                        conn=conn,
                    )

            completed += len(batch)
            previous_usage = ctx.data.get("embedding_usage", {})
            await ctx.commit(
                {
                    "embedded": [*ctx.data.get("embedded", []), *(item["marker"] for item in batch)],
                    "embedding_usage": {
                        "requests": previous_usage.get("requests", 0) + 1,
                        "input_tokens": previous_usage.get("input_tokens", 0)
                        + (usage.get("prompt_tokens", 0) or 0),
                    },
                    "embedding_completed": completed,
                    "embedding_activity": None,
                    "phase": "生成教材向量索引",
                },
                write,
            )
        await latest_embedding()

    def ready(conn: Connection) -> None:
        current = ctx.db.get("book", book["id"], conn=conn) or book
        ctx.db.put(
            "book",
            {**current, "status": "ready", "embedding_pending": embedding is None},
            id=book["id"],
            conn=conn,
        )

    await ctx.finish({"book_id": book["id"], "embedding_configured": embedding is not None}, ready)


async def suggestion_regenerate(ctx: PipelineContext) -> None:
    from studyquip.retrieval import records
    from studyquip.textbook import TextbookService

    suggestion = await asyncio.to_thread(ctx.db.get, "suggestion", ctx.job["resource_id"])
    if not suggestion:
        raise ValueError("建议不存在")
    profile = await ctx.ai.profile_for("book_text")
    book_id = suggestion["book_id"]
    nodes = await asyncio.to_thread(records, ctx.db, "node", {"book_id": book_id})
    prompt = (
        "根据当前教材版本重新生成修改建议。原操作已过期或需要重新评估；先使用工具读取受影响块的最新版本。只提交必要操作，不覆盖人工修改；工作记忆为空即可。\n"
        + json.dumps({"previous_suggestion": suggestion, "outline": nodes}, ensure_ascii=False)
    )
    draft: RevisionDraft = await ctx.structured(
        "suggestion_regenerate",
        profile,
        prompt,
        RevisionDraft,
        tools=retrieval_tools(ctx, [book_id], context_tokens=profile.context_tokens),
    )
    group_id = ctx.data.get("group_id") or str(uuid.uuid4())
    await ctx.commit({"group_id": group_id})

    def write(conn: Connection) -> None:
        current = ctx.db.get("suggestion", suggestion["id"], conn=conn)
        if not current or current["revision"] != suggestion["revision"]:
            raise ConflictError("原建议在重新生成期间已变化")
        operations = wire_operations(draft, "")
        for operation in operations:
            if operation["op"] == "insert":
                operation["block"]["source_page_ids"] = [
                    value for value in operation["block"]["source_page_ids"] if value
                ]
        receipt = TextbookService(ctx.db).apply_operations(
            book_id, group_id, operations, draft.reason, actor="ai", conn=conn
        )
        ctx.db.put(
            "suggestion",
            {**current, "status": "stale", "replacement_receipt": receipt},
            id=current["id"],
            conn=conn,
        )

    await ctx.finish({"suggestion_id": suggestion["id"], "operation_group_id": group_id}, write)


async def query_embedding(ctx: PipelineContext, query: str) -> tuple[list[float], str]:
    from studyquip.retrieval import embedding_fingerprint

    embedding = await ctx.ai.profile_for("embedding")
    space = embedding_fingerprint(embedding.model_dump(), embedding.embedding_dimensions or 0)
    cache_key = hashlib.sha256((space + query).encode()).hexdigest()
    cached = ctx.data.get("query_embeddings", {}).get(cache_key)
    if cached:
        return cached["vector"], cached["space_fingerprint"]

    async def activity(value: Json) -> None:
        await ctx.commit(
            {"embedding_activity": {**value, "model": embedding.model, "revision": embedding.revision}}
        )

    vectors, usage = await ctx.ai.embed(
        embedding,
        [query],
        query=True,
        before_request=ctx.guard,
        bypass_window=bool(ctx.job.get("bypass_window")),
        activity=activity,
    )
    latest = await ctx.ai.profile_for("embedding")
    if embedding_fingerprint(latest.model_dump(), latest.embedding_dimensions or 0) != space:
        from studyquip.scheduling import WindowClosed

        raise WindowClosed(time.time(), "嵌入空间配置已变化，按最新配置重新检索")
    fingerprint = embedding_fingerprint(embedding.model_dump(), len(vectors[0]))
    previous = ctx.data.get("query_embedding_usage", {})
    await ctx.commit(
        {
            "query_embeddings": {
                **ctx.data.get("query_embeddings", {}),
                cache_key: {"vector": vectors[0], "space_fingerprint": fingerprint},
            },
            "query_embedding_usage": {
                "requests": previous.get("requests", 0) + 1,
                "input_tokens": previous.get("input_tokens", 0) + (usage.get("prompt_tokens", 0) or 0),
            },
            "embedding_activity": None,
        }
    )
    return vectors[0], fingerprint


async def execute_search(ctx: PipelineContext, request: "SearchInput") -> list[Json]:
    from studyquip.search import append_stage, current_hits, search_stage

    candidates: list[Json] = []
    for step, method in enumerate(request.methods, 1):
        await ctx.guard()
        vector, fingerprint = (
            await query_embedding(ctx, request.query)
            if method == "semantic" and request.query.strip()
            else (None, None)
        )
        hits = await asyncio.to_thread(
            search_stage, ctx.db, request, method, query_vector=vector, space_fingerprint=fingerprint
        )
        candidates.extend({**hit, "step": step} for hit in hits)
    valid = await asyncio.to_thread(current_hits, ctx.db, request, candidates)
    output: list[Json] = []
    for hit in valid:
        append_stage(output, [hit], hit["step"])
    return output


async def search_job(ctx: PipelineContext) -> None:
    from studyquip.schemas import SearchInput

    query = await asyncio.to_thread(ctx.db.get, "search", ctx.job["resource_id"])
    if query is None:
        raise ValueError("搜索任务输入不存在")
    request = SearchInput.model_validate(query)
    hits = await execute_search(ctx, request)
    await ctx.finish({"hits": hits, "methods": request.methods})


async def question_index(ctx: PipelineContext) -> None:
    from studyquip.question_index import QuestionIndex
    from studyquip.retrieval import embedding_fingerprint
    from studyquip.scheduling import WindowClosed

    service = QuestionIndex(ctx.db)
    question_id = ctx.job["resource_id"]
    # The index follows the latest sections; it never writes a question revision.
    while True:

        def snapshot() -> tuple[Json | None, list[Json], int]:
            with ctx.db.read() as conn:
                profile = service.profile(conn)
                targets, _ = service.eligible(conn, question_id)
                return profile, service.pending(question_id, profile, conn) if profile else [], len(targets)

        profile_data, pending, total = await asyncio.to_thread(snapshot)
        completed = total - len(pending) if profile_data else 0
        await ctx.commit(
            {
                "phase": "更新题目分部向量索引" if profile_data else "等待配置嵌入模型",
                "embedding_total": total,
                "embedding_completed": completed,
            }
        )
        space = (
            embedding_fingerprint(profile_data, profile_data.get("embedding_dimensions") or 0)
            if profile_data
            else None
        )
        if not profile_data or not pending:

            def finish_latest(conn: Connection) -> None:
                latest = service.profile(conn)
                if latest and service.pending(question_id, latest, conn):
                    raise WindowClosed(time.time(), "题目或嵌入配置已更新，继续补齐最新部分")

            await ctx.finish(
                {"question_id": question_id, "embedding_configured": bool(profile_data)}, finish_latest
            )
            return
        profile = ModelProfile.model_validate(profile_data)
        batch: list[Json] = []
        for part in pending:
            if batch and (
                len(batch) >= ctx.settings.question_embedding_batch_size
                or (
                    profile.context_tokens is not None
                    and estimate_tokens([item["text"] for item in [*batch, part]]) > profile.context_tokens
                )
            ):
                break
            batch.append(part)

        async def activity(value: Json) -> None:
            await ctx.commit(
                {"embedding_activity": {**value, "model": profile.model, "revision": profile.revision}}
            )

        async def before_embedding() -> None:
            await ctx.guard()
            latest = await ctx.ai.profile_for("embedding")
            if embedding_fingerprint(latest.model_dump(), latest.embedding_dimensions or 0) != space:
                raise WindowClosed(time.time(), "嵌入空间已变化，按最新配置继续题目索引")

        vectors, usage = await ctx.ai.embed(
            profile,
            [part["text"] for part in batch],
            before_request=before_embedding,
            bypass_window=bool(ctx.job.get("bypass_window")),
            activity=activity,
        )
        fingerprint = embedding_fingerprint(profile_data, len(vectors[0]))

        def publish(conn: Connection) -> None:
            latest = service.profile(conn)
            if not latest or embedding_fingerprint(latest, latest.get("embedding_dimensions") or 0) != space:
                return  # Count the completed request, discard its obsolete space, then re-plan.
            for part, vector in zip(batch, vectors, strict=True):
                service.store(part, fingerprint, vector, conn)

        previous = ctx.data.get("embedding_usage", {})
        await ctx.commit(
            {
                "embedding_activity": None,
                "embedding_usage": {
                    "requests": previous.get("requests", 0) + 1,
                    "input_tokens": previous.get("input_tokens", 0) + (usage.get("prompt_tokens", 0) or 0),
                },
            },
            publish,
        )


HANDLERS: dict[str, Callable[[PipelineContext], Any]] = {
    "model_test": model_test,
    "question_extract": question_extract,
    "question_explain": question_explain,
    "question_audio": question_audio,
    "book_process": book_process,
    "page_recognize": page_recognize,
    "book_index": book_index,
    "question_index": question_index,
    "suggestion_regenerate": suggestion_regenerate,
    "export_pdf": export_pdf,
    "search": search_job,
}
