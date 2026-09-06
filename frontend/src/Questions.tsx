import { useEffect, useRef, useState } from "react";
import { ArrowUpRight, BookOpen, Plus } from "lucide-react";
import { v4 as uuidv4 } from "uuid";
import { api, post, put, remove } from "./api";
import {
  answerText,
  questionTypes,
  type Book,
  type Job,
  type Question,
  type QuestionType,
  type Subject,
} from "./types";
import {
  Badge,
  Button,
  Check,
  Field,
  Modal,
  ScheduleDialog,
  State,
  moveItem,
  useNotice,
  useRemote,
} from "./ui";
import { MathText, QuestionContent } from "./Content";
import Uploads from "./Uploads";
import {
  ResourceProgress,
  useResourceJobs,
  useJobs,
  isActiveJob,
  useJobCompletion,
} from "./JobProgress";
export function emptyQuestion(subjectId: string): Question {
  return {
    id: "",
    revision: 0,
    subject_id: subjectId,
    type: "single_choice",
    stem: "",
    options: [
      { id: uuidv4(), text: "" },
      { id: uuidv4(), text: "" },
    ],
    answer: "",
    answer_confirmed: false,
    wrong_answer: "",
    error_reason: "",
    optimize_error_reason: false,
    notes: "",
    reference_text: "",
    asset_ids: [],
    reference_asset_ids: [],
    figure_asset_ids: [],
    book_ids: [],
  };
}
export default function Questions({
  subjects,
  books,
  startNew = false,
  initialQuestionId = null,
}: {
  subjects: Subject[];
  books: Book[];
  startNew?: boolean;
  initialQuestionId?: string | null;
}) {
  const remote = useRemote<Question[]>("/questions", []);
  const tasks = useJobs();
  useJobCompletion(tasks.jobs, remote.reload);
  const [search, setSearch] = useState(""),
    [subject, setSubject] = useState(""),
    [type, setType] = useState(""),
    [editor, setEditor] = useState<Question | null>(() =>
      startNew ? emptyQuestion(subjects[0]?.id || "") : null,
    ),
    [selected, setSelected] = useState<string[]>([]),
    [exporting, setExporting] = useState(false);
  const openedInitial = useRef(false);
  const notice = useNotice();
  useEffect(() => {
    if (
      !initialQuestionId ||
      openedInitial.current ||
      remote.loading ||
      remote.error
    )
      return;
    openedInitial.current = true;
    const question = remote.data.find((item) => item.id === initialQuestionId);
    if (question) setEditor(question);
    else notice("这道错题已不存在，请在列表中选择其他题目。", true);
  }, [initialQuestionId, remote.data, remote.loading, remote.error, notice]);
  const questions = remote.data.filter(
    (q) =>
      (!subject || q.subject_id === subject) &&
      (!type || q.type === type) &&
      (!search ||
        [q.stem, q.error_reason, q.notes, q.reference_text].some((t) =>
          t?.toLowerCase().includes(search.toLowerCase()),
        )),
  );
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>错题</h1>
          <p>整理题目，理解每一次错误。</p>
        </div>
        <div className="inline-actions">
          <Button onClick={remote.reload}>刷新</Button>
          <Button
            disabled={!selected.length}
            onClick={() => setExporting(true)}
          >
            导出 PDF{selected.length ? ` · ${selected.length}` : ""}
          </Button>
          <Button
            kind="primary"
            onClick={() => setEditor(emptyQuestion(subjects[0]?.id || ""))}
          >
            <Plus size={16} aria-hidden="true" />
            录入错题
          </Button>
        </div>
      </div>
      {tasks.jobs
        .filter((job) => job.kind === "export_pdf" && isActiveJob(job))
        .map((job) => (
          <ResourceProgress key={job.id} resourceId={job.resource_id} />
        ))}
      {books.length === 0 && (
        <div className="gentle-notice library-notice">
          <BookOpen size={18} strokeWidth={1.7} aria-hidden="true" />
          <p>还没有教材。可以先在教材库导入资料，让讲解有据可依。</p>
          <a href="#books?new=1">
            导入教材
            <ArrowUpRight size={14} aria-hidden="true" />
          </a>
        </div>
      )}
      <div className="filter-bar">
        <input
          aria-label="搜索错题"
          placeholder="搜索题目、做错原因或备注…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select
          aria-label="科目筛选"
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
        >
          <option value="">全部科目</option>
          {subjects.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
        <select
          aria-label="题型筛选"
          value={type}
          onChange={(e) => setType(e.target.value)}
        >
          <option value="">全部题型</option>
          {Object.entries(questionTypes).map(([key, name]) => (
            <option key={key} value={key}>
              {name}
            </option>
          ))}
        </select>
      </div>
      <State
        loading={remote.loading}
        error={remote.error}
        empty={!questions.length}
      >
        还没有符合条件的错题。点击“录入错题”开始。
      </State>
      {questions.length > 0 && (
        <div className="record-list">
          <div className="list-label">
            <Check
              label="选择当前列表"
              checked={
                questions.length > 0 &&
                questions.every((q) => selected.includes(q.id))
              }
              onChange={(v) =>
                setSelected(
                  v
                    ? [...new Set([...selected, ...questions.map((q) => q.id)])]
                    : selected.filter(
                        (id) => !questions.some((q) => q.id === id),
                      ),
                )
              }
            />
            <span>{questions.length} 道题</span>
          </div>
          {questions.map((q) => (
            <div className="question-row" key={q.id}>
              <input
                aria-label="选择此题导出"
                type="checkbox"
                checked={selected.includes(q.id)}
                onChange={(e) =>
                  setSelected(
                    e.target.checked
                      ? [...selected, q.id]
                      : selected.filter((id) => id !== q.id),
                  )
                }
              />
              <button className="record-main" onClick={() => setEditor(q)}>
                <div className="record-meta">
                  <span>
                    {subjects.find((s) => s.id === q.subject_id)?.name ||
                      "未设置科目"}
                  </span>
                  <span>{questionTypes[q.type]}</span>
                  <Badge
                    status={q.answer_confirmed ? "ready" : "needs_review"}
                  />
                  {q.explanation_stale && <span>讲解待更新</span>}
                </div>
                <MathText
                  className="line-clamp"
                  text={q.stem || "待整理的题目"}
                />
                {q.notes && <p className="record-note">{q.notes}</p>}
              </button>
              <ArrowUpRight
                className="row-arrow"
                size={18}
                strokeWidth={1.7}
                aria-hidden="true"
              />
            </div>
          ))}
        </div>
      )}
      {editor && (
        <QuestionEditor
          initial={editor}
          subjects={subjects}
          books={books}
          onClose={() => setEditor(null)}
          onSaved={() => remote.reload()}
        />
      )}{" "}
      {exporting && (
        <ExportDialog
          initial={selected
            .map((id) => remote.data.find((q) => q.id === id))
            .filter((q): q is Question => !!q)}
          onClose={() => setExporting(false)}
        />
      )}
    </>
  );
}
function AnswerInput({
  question,
  value,
  onChange,
  label,
}: {
  question: Question;
  value: unknown;
  onChange: (value: unknown) => void;
  label: string;
}) {
  if (question.type === "single_choice")
    return (
      <Field label={label}>
        <select
          value={typeof value === "string" ? value : ""}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">请选择</option>
          {question.options.map((o, i) => (
            <option key={o.id} value={o.id}>
              {String.fromCharCode(65 + i)} ·{" "}
              {o.text.slice(0, 40) || "待填写选项"}
            </option>
          ))}
        </select>
      </Field>
    );
  if (question.type === "multiple_choice") {
    const current = Array.isArray(value) ? value : [];
    return (
      <div className="field">
        <span>{label}</span>
        <div className="choice-inputs">
          {question.options.map((o, i) => (
            <Check
              key={o.id}
              label={`${String.fromCharCode(65 + i)} · ${o.text.slice(0, 30) || "待填写选项"}`}
              checked={current.includes(o.id)}
              onChange={(checked) =>
                onChange(
                  checked
                    ? [...current, o.id]
                    : current.filter((id) => id !== o.id),
                )
              }
            />
          ))}
        </div>
      </div>
    );
  }
  return (
    <Field
      label={label}
      hint={
        question.type === "fill_blank"
          ? "每行对应一个空位。"
          : "支持文本与 LaTeX 公式。"
      }
    >
      <textarea
        rows={question.type === "fill_blank" ? 3 : 4}
        value={Array.isArray(value) ? value.join("\n") : answerText(value)}
        onChange={(e) =>
          onChange(
            question.type === "fill_blank"
              ? e.target.value.split("\n")
              : e.target.value,
          )
        }
      />
    </Field>
  );
}
function QuestionEditor({
  initial,
  subjects,
  books,
  onClose,
  onSaved,
}: {
  initial: Question;
  subjects: Subject[];
  books: Book[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [q, setQ] = useState<Question>(structuredClone(initial)),
    [busy, setBusy] = useState(false),
    [dirty, setDirty] = useState(false),
    [schedule, setSchedule] = useState<"extract" | "explain" | null>(null),
    [preview, setPreview] = useState(false);
  const notice = useNotice();
  const tasks = useResourceJobs(q.id, ["question_extract", "question_explain"]);
  useJobCompletion(tasks.jobs, () => {
    if (!dirty && q.id)
      void api<Question>(`/questions/${q.id}`)
        .then(setQ)
        .catch((e) => notice(e.message, true));
    onSaved();
  });
  function update<K extends keyof Question>(key: K, value: Question[K]) {
    setDirty(true);
    setQ((old) => ({
      ...old,
      [key]: value,
      ...(old.explanation ? { explanation_stale: true } : {}),
      ...(["answer", "type", "options", "stem"].includes(key)
        ? { answer_confirmed: false }
        : {}),
    }));
  }
  async function save(confirm = false): Promise<Question> {
    if (!q.subject_id)
      throw new Error("请先在设置中添加科目，并为题目选择科目。");
    let saved = q.id
      ? await put<Question>(`/questions/${q.id}`, q)
      : await post<Question>("/questions", q);
    setQ(saved);
    setDirty(false);
    onSaved();
    if (confirm) {
      saved = await post<Question>(`/questions/${saved.id}/confirm`, {
        revision: saved.revision,
      });
      setQ(saved);
      onSaved();
    }
    return saved;
  }
  async function handleSave(confirm = false) {
    setBusy(true);
    try {
      await save(confirm);
      notice(confirm ? "正确答案已确认。" : "题目已保存。");
    } catch (e) {
      notice((e as Error).message, true);
    } finally {
      setBusy(false);
    }
  }
  function close() {
    if (!dirty || window.confirm("有尚未保存的修改，确定关闭吗？")) onClose();
  }
  const matchingBooks = books.filter((b) => b.subject_id === q.subject_id);
  return (
    <Modal title={q.id ? "编辑错题" : "录入错题"} onClose={close} wide>
      <div className="editor-toolbar">
        <div className="inline-actions">
          <Button
            kind={!preview ? "active" : ""}
            onClick={() => setPreview(false)}
          >
            编辑
          </Button>
          <Button
            kind={preview ? "active" : ""}
            onClick={() => setPreview(true)}
          >
            预览
          </Button>
        </div>
        <div className="inline-actions">
          {q.id && (
            <Button
              onClick={async () => {
                if (
                  dirty &&
                  !window.confirm("重新载入会放弃未保存内容，继续吗？")
                )
                  return;
                try {
                  setQ(await api<Question>(`/questions/${q.id}`));
                  setDirty(false);
                } catch (e) {
                  notice((e as Error).message, true);
                }
              }}
            >
              载入最新结果
            </Button>
          )}
          <Button
            disabled={busy || tasks.blocked}
            onClick={() => setSchedule("extract")}
          >
            {tasks.active.length ? "题目处理中" : "AI 整理题目"}
          </Button>
        </div>
      </div>
      {q.id && (
        <ResourceProgress
          resourceId={q.id}
          kinds={["question_extract", "question_explain"]}
        />
      )}
      {preview ? (
        <QuestionContent question={q} answer explanation knowledge />
      ) : (
        <div className="editor-grid">
          <div className="stack">
            <div className="form-grid">
              <Field label="科目">
                <select
                  value={q.subject_id}
                  onChange={(e) => {
                    update("subject_id", e.target.value);
                    update("book_ids", []);
                  }}
                >
                  <option value="">选择科目</option>
                  {subjects.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="题型">
                <select
                  value={q.type}
                  onChange={(e) => {
                    const type = e.target.value as QuestionType;
                    update("type", type);
                    update("answer", "");
                  }}
                >
                  {Object.entries(questionTypes).map(([key, name]) => (
                    <option key={key} value={key}>
                      {name}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            <Uploads
              label="题目原始材料"
              ids={q.asset_ids || []}
              onChange={(ids) => update("asset_ids", ids)}
              onCrop={(id) =>
                update("figure_asset_ids", [...(q.figure_asset_ids || []), id])
              }
            />
            <Field label="题干">
              <textarea
                className="stem-input"
                value={q.stem}
                onChange={(e) => update("stem", e.target.value)}
                placeholder="输入题目，或上传图片后使用 AI 整理。"
              />
            </Field>
            {["single_choice", "multiple_choice"].includes(q.type) && (
              <div className="stack compact">
                <div className="row-between">
                  <span className="field-label">选项</span>
                  <Button
                    onClick={() =>
                      update("options", [
                        ...q.options,
                        { id: uuidv4(), text: "" },
                      ])
                    }
                  >
                    ＋ 添加选项
                  </Button>
                </div>
                {q.options.map((option, i) => (
                  <div className="option-row" key={option.id}>
                    <span>{String.fromCharCode(65 + i)}</span>
                    <textarea
                      rows={1}
                      aria-label={`选项${String.fromCharCode(65 + i)}`}
                      value={option.text}
                      onChange={(e) =>
                        update(
                          "options",
                          q.options.map((o) =>
                            o.id === option.id
                              ? { ...o, text: e.target.value }
                              : o,
                          ),
                        )
                      }
                    />
                    <Button
                      aria-label={`删除选项${i + 1}`}
                      onClick={() =>
                        update(
                          "options",
                          q.options.filter((o) => o.id !== option.id),
                        )
                      }
                    >
                      移除
                    </Button>
                  </div>
                ))}
              </div>
            )}
            <Uploads
              label="练习题配图"
              ids={q.figure_asset_ids || []}
              onChange={(ids) => update("figure_asset_ids", ids)}
            />
            <div className="answer-panel">
              <div className="row-between">
                <h3>正确答案</h3>
                <span
                  className={q.answer_confirmed ? "confirmed" : "unconfirmed"}
                >
                  {q.answer_confirmed ? "已由你确认" : "需要你确认"}
                </span>
              </div>
              <AnswerInput
                question={q}
                value={q.answer}
                onChange={(value) => update("answer", value)}
                label="答案内容"
              />
              <p className="hint">
                可从参考解析提取答案；生成讲解前，必须由你检查并确认。
              </p>
              <Button busy={busy} onClick={() => void handleSave(true)}>
                保存并确认答案
              </Button>
            </div>
          </div>
          <aside className="editor-aside stack">
            <div>
              <h3>参考教材</h3>
              <p className="hint">不选择时，自动检索同科目的全部教材。</p>
              <div className="book-choices">
                {matchingBooks.map((book) => (
                  <Check
                    key={book.id}
                    label={book.title}
                    checked={q.book_ids?.includes(book.id) || false}
                    onChange={(v) =>
                      update(
                        "book_ids",
                        v
                          ? [...(q.book_ids || []), book.id]
                          : q.book_ids.filter((id) => id !== book.id),
                      )
                    }
                  />
                ))}
              </div>
            </div>
            <details open>
              <summary>参考答案与解析</summary>
              <div className="stack">
                <Field label="参考解析文本">
                  <textarea
                    rows={5}
                    value={q.reference_text || ""}
                    onChange={(e) => update("reference_text", e.target.value)}
                    placeholder="可粘贴标准答案及解析。"
                  />
                </Field>
                <Uploads
                  label="参考解析图片"
                  ids={q.reference_asset_ids || []}
                  onChange={(ids) => update("reference_asset_ids", ids)}
                />
              </div>
            </details>
            <details>
              <summary>我的原错误作答</summary>
              <AnswerInput
                question={q}
                value={q.wrong_answer}
                onChange={(value) => update("wrong_answer", value)}
                label="原作答（可留空）"
              />
            </details>
            <div className="stack compact">
              <Field label="做错原因（可留空）">
                <textarea
                  rows={4}
                  value={q.error_reason ?? ""}
                  onChange={(e) => update("error_reason", e.target.value)}
                  placeholder="回想当时为什么做错，例如漏看条件、概念混淆或计算失误。"
                />
              </Field>
              <Check
                label="让 AI 优化表述"
                checked={q.optimize_error_reason ?? false}
                onChange={(value) => update("optimize_error_reason", value)}
              />
              <p className="hint">生成讲解时优化表达，保留你的原文。</p>
            </div>
            <Field label="备注">
              <textarea
                rows={4}
                value={q.notes || ""}
                onChange={(e) => update("notes", e.target.value)}
                placeholder="告诉 AI 需要注意的地方。"
              />
            </Field>
          </aside>
        </div>
      )}
      {q.explanation && !preview && (
        <details className="generated-explanation" open>
          <summary>
            已生成讲解{q.explanation_stale ? " · 题目已修改，需要重新生成" : ""}
          </summary>
          <QuestionContent
            question={{ ...q, stem: "", options: [], figure_asset_ids: [] }}
            explanation
            knowledge
          />
        </details>
      )}
      <div className="editor-footer">
        <div>
          {q.id && (
            <Button
              kind="danger"
              onClick={async () => {
                if (!window.confirm("确定删除这道错题吗？")) return;
                try {
                  await remove(`/questions/${q.id}`);
                  onSaved();
                  onClose();
                } catch (e) {
                  notice((e as Error).message, true);
                }
              }}
            >
              删除题目
            </Button>
          )}
        </div>
        <div className="inline-actions">
          <Button busy={busy} onClick={() => void handleSave()}>
            保存
          </Button>
          <Button
            kind="primary"
            disabled={!q.answer_confirmed || dirty || tasks.blocked}
            onClick={() => setSchedule("explain")}
          >
            {tasks.active.length ? "已有处理任务" : "生成讲解"}
          </Button>
        </div>
      </div>
      {schedule && (
        <ScheduleDialog
          title={schedule === "extract" ? "AI 整理题目" : "生成讲解"}
          onClose={() => setSchedule(null)}
          onSubmit={async (timing) => {
            const saved = dirty || !q.id ? await save() : q;
            return post<Job>(`/questions/${saved.id}/${schedule}`, timing);
          }}
        />
      )}
    </Modal>
  );
}
function ExportDialog({
  initial,
  onClose,
}: {
  initial: Question[];
  onClose: () => void;
}) {
  const [questions, setQuestions] = useState(initial),
    [mode, setMode] = useState<"practice" | "review">("practice"),
    [answer, setAnswer] = useState(true),
    [explanation, setExplanation] = useState(true),
    [knowledge, setKnowledge] = useState(true),
    [blank, setBlank] = useState(5),
    [busy, setBusy] = useState(false);
  const notice = useNotice();
  const tasks = useJobs();
  const pending = tasks.jobs.find(
    (job) =>
      job.kind === "export_pdf" &&
      isActiveJob(job) &&
      job.question_ids?.some((id) => questions.some((q) => q.id === id)),
  );
  return (
    <Modal title="导出 PDF" onClose={onClose} wide>
      <div className="export-grid">
        <div className="stack">
          <Field label="导出模式">
            <select
              value={mode}
              onChange={(e) => setMode(e.target.value as typeof mode)}
            >
              <option value="practice">练习版</option>
              <option value="review">复习版</option>
            </select>
          </Field>
          {mode === "review" && (
            <>
              <Check
                label="包含正确答案"
                checked={answer}
                onChange={setAnswer}
              />
              <Check
                label="包含解析"
                checked={explanation}
                onChange={setExplanation}
              />
              <Check
                label="包含知识点"
                checked={knowledge}
                onChange={setKnowledge}
              />
            </>
          )}
          <Field label="每题作答留白行数">
            <input
              type="number"
              min="0"
              max="40"
              value={blank}
              onChange={(e) => setBlank(Number(e.target.value))}
            />
          </Field>
          <h3>题目顺序</h3>
          {questions.map((q, i) => (
            <div className="export-order" key={q.id}>
              <span>
                {i + 1}. {q.stem.slice(0, 34) || "题目"}
              </span>
              <Button
                disabled={i === 0}
                onClick={() => setQuestions(moveItem(questions, i, -1))}
              >
                ↑
              </Button>
              <Button
                disabled={i === questions.length - 1}
                onClick={() => setQuestions(moveItem(questions, i, 1))}
              >
                ↓
              </Button>
            </div>
          ))}
          <Button
            kind="primary"
            busy={busy}
            disabled={!tasks.ready || !!tasks.error || !!pending}
            onClick={async () => {
              setBusy(true);
              try {
                await post<Job>("/exports", {
                  question_ids: questions.map((q) => q.id),
                  mode,
                  include_answer: mode === "review" && answer,
                  include_explanation: mode === "review" && explanation,
                  include_knowledge: mode === "review" && knowledge,
                  blank_lines: blank,
                });
                notice("PDF 导出任务已提交。完成后在任务页下载。");
                onClose();
              } catch (e) {
                notice((e as Error).message, true);
              } finally {
                setBusy(false);
              }
            }}
          >
            生成 PDF
          </Button>
        </div>
        <div className="paper-preview">
          <p className="preview-label">内容预览 · 最终以 PDF 分页为准</p>
          {questions.map((q, i) => (
            <QuestionContent
              key={q.id}
              question={q}
              index={i}
              answer={mode === "review" && answer}
              explanation={mode === "review" && explanation}
              knowledge={mode === "review" && knowledge}
              blankLines={blank}
            />
          ))}
        </div>
      </div>
    </Modal>
  );
}
