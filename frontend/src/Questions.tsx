import ExportDialog from "./QuestionExport";
import { useEffect, useRef, useState } from "react";
import { ArrowUpRight, BookOpen, Plus } from "lucide-react";
import { v4 as uuidv4 } from "uuid";
import { api, post, put, remove } from "./api";
import {
  answerText,
  questionTypes,
  questionNodes,
  questionTitle,
  roleLabels,
  type Book,
  type Job,
  type Model,
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
  useNotice,
  useRemote,
} from "./ui";
import { MathText, QuestionContent } from "./Content";
import Uploads from "./Uploads";
import {
  atPath,
  changeAt,
  FiguresEditor,
  MaterialsEditor,
  QuestionNavigator,
} from "./QuestionExtras";
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
    parts: [],
    materials: [],
    rendered_figures: [],
    figure_requirements: [],
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
        questionNodes(q)
          .flatMap((node) => [
            node.stem,
            node.error_reason,
            node.notes,
            node.reference_text,
            ...(node.materials || []).flatMap((material) => [
              material.title,
              material.text,
            ]),
          ])
          .some((t) => t?.toLowerCase().includes(search.toLowerCase()))),
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
                <MathText className="line-clamp" text={questionTitle(q)} />
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
          subjects={subjects}
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
  const [root, setRoot] = useState<Question>(structuredClone(initial)),
    [busy, setBusy] = useState(false),
    [dirty, setDirty] = useState(false),
    [schedule, setSchedule] = useState<"extract" | "explain" | "audio" | null>(
      null,
    ),
    [path, setPath] = useState<number[]>([]),
    [preview, setPreview] = useState(false);
  const q = atPath(root, path);
  const models = useRemote<Model[]>("/models", []);
  const notice = useNotice();
  const tasks = useResourceJobs(root.id, [
    "question_extract",
    "question_explain",
    "question_audio",
  ]);
  useJobCompletion(tasks.jobs, () => {
    if (!dirty && root.id)
      void api<Question>(`/questions/${root.id}`)
        .then(setRoot)
        .catch((e) => notice(e.message, true));
    onSaved();
  });
  function replace(value: Question) {
    setDirty(true);
    setRoot({ ...value, answer_confirmed: false, explanation_stale: true });
  }
  function update<K extends keyof Question>(key: K, value: Question[K]) {
    setDirty(true);
    setRoot((old) => ({
      ...changeAt(old, path, (node) => ({
        ...node,
        [key]: value,
        formatting_warnings: ["stem", "options"].includes(key)
          ? []
          : node.formatting_warnings,
        explanation_stale: !!node.explanation,
      })),
      answer_confirmed: [
        "answer",
        "type",
        "options",
        "stem",
        "asset_ids",
        "reference_text",
        "reference_asset_ids",
        "materials",
        "rendered_figures",
        "figure_asset_ids",
        "parts",
      ].includes(key)
        ? false
        : old.answer_confirmed,
      explanation_stale: true,
    }));
  }
  function updateRoot<K extends keyof Question>(key: K, value: Question[K]) {
    setDirty(true);
    setRoot((old) => ({
      ...old,
      [key]: value,
      explanation_stale: true,
    }));
  }
  async function save(confirm = false): Promise<Question> {
    if (!root.subject_id)
      throw new Error("请先在设置中添加科目，并为题目选择科目。");
    let saved = root.id
      ? await put<Question>(`/questions/${root.id}`, root)
      : await post<Question>("/questions", root);
    setRoot(saved);
    setDirty(false);
    onSaved();
    if (confirm) {
      saved = await post<Question>(`/questions/${saved.id}/confirm`, {
        revision: saved.revision,
      });
      setRoot(saved);
      onSaved();
    }
    return saved;
  }
  async function handleSave(confirm = false) {
    setBusy(true);
    try {
      const saved = await save(confirm);
      const needed = saved.audio_pending_roles?.some((role) =>
        models.data.some((model) => model.role === role),
      );
      if (needed && !tasks.blocked) {
        await post<Job>(`/questions/${saved.id}/audio`);
        tasks.reload();
      }
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
  const matchingBooks = books.filter((b) => b.subject_id === root.subject_id);
  return (
    <Modal title={root.id ? "编辑错题" : "录入错题"} onClose={close} wide>
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
          {root.id && (
            <Button
              onClick={async () => {
                if (
                  dirty &&
                  !window.confirm("重新载入会放弃未保存内容，继续吗？")
                )
                  return;
                try {
                  setRoot(await api<Question>(`/questions/${root.id}`));
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
          {questionNodes(root).some((node) =>
            node.materials?.some((material) => material.kind === "listening"),
          ) && (
            <Button
              disabled={busy || tasks.blocked}
              onClick={() => setSchedule("audio")}
            >
              补全听力资料
            </Button>
          )}
        </div>
      </div>
      {root.id && (
        <ResourceProgress
          resourceId={root.id}
          kinds={[
            "question_extract",
            "question_explain",
            "question_audio",
            "question_index",
          ]}
        />
      )}
      <p className="hint">
        AI
        整理可润色题干和选项、规范公式，并按需检索教材；保留原题意、条件与数值。完成后可切换预览，检查并重新确认答案。
      </p>
      {!!q.formatting_warnings?.length && (
        <div className="gentle-notice" role="status">
          <strong>表述需要核对</strong>
          <ul>
            {q.formatting_warnings.map((message, index) => (
              <li key={index}>{message}</li>
            ))}
          </ul>
        </div>
      )}
      {!!root.audio_pending_roles?.length && (
        <p className="gentle-notice">
          听力待补全：
          {root.audio_pending_roles
            .map(
              (role) =>
                `${roleLabels[role]}${models.data.some((model) => model.role === role) ? "已配置" : "未配置"}`,
            )
            .join("、")}
          。未配置时保留原材料，可到设置补充后继续。
        </p>
      )}
      {!preview && (
        <QuestionNavigator
          root={root}
          path={path}
          onSelect={setPath}
          onChange={replace}
          create={() => ({ ...emptyQuestion(root.subject_id), id: uuidv4() })}
        />
      )}
      {preview ? (
        <QuestionContent question={root} answer explanation knowledge />
      ) : (
        <div className="editor-grid">
          <div className="stack">
            <div className="form-grid">
              {path.length === 0 && (
                <Field label="科目">
                  <select
                    value={root.subject_id}
                    onChange={(e) => {
                      updateRoot("subject_id", e.target.value);
                      updateRoot("book_ids", []);
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
              )}
              <Field label="题型">
                <select
                  value={q.type}
                  onChange={(e) => {
                    const type = e.target.value as QuestionType;
                    if (type !== "composite" && q.parts?.length) {
                      notice(
                        "请先移动或删除子题，再将此节点改为基础题型。",
                        true,
                      );
                      return;
                    }
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
            <Field label={q.type === "composite" ? "大题题干（可选）" : "题干"}>
              <textarea
                className="stem-input"
                value={q.stem}
                onChange={(e) => update("stem", e.target.value)}
                placeholder="输入题目，或上传图片后使用 AI 整理。"
              />
            </Field>
            <MaterialsEditor
              value={q.materials || []}
              onChange={(value) => update("materials", value)}
            />
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
              label="题目插图（可选）"
              ids={q.figure_asset_ids || []}
              onChange={(ids) => update("figure_asset_ids", ids)}
            />
            <FiguresEditor
              value={q.rendered_figures || []}
              onChange={(value) => update("rendered_figures", value)}
            />
            {!!q.figure_requirements?.length && (
              <div className="gentle-notice">
                <strong>需要补充插图</strong>
                <ul>
                  {q.figure_requirements.map((message, i) => (
                    <li key={i}>{message}</li>
                  ))}
                </ul>
                <Button onClick={() => update("figure_requirements", [])}>
                  已核对，现有插图足够
                </Button>
              </div>
            )}
            {q.type !== "composite" && (
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
                <Button
                  busy={busy}
                  disabled={tasks.blocked}
                  onClick={() => void handleSave(true)}
                >
                  保存并确认全部答案
                </Button>
              </div>
            )}
          </div>
          <aside className="editor-aside stack">
            <details>
              <summary>
                参考教材 · 可选
                {root.book_ids.length
                  ? ` · 已选 ${root.book_ids.length} 本`
                  : ""}
              </summary>
              <p className="hint">不选择时，自动检索同科目的全部教材。</p>
              <div className="book-choices">
                {matchingBooks.map((book) => (
                  <Check
                    key={book.id}
                    label={book.title}
                    checked={root.book_ids?.includes(book.id) || false}
                    onChange={(v) =>
                      updateRoot(
                        "book_ids",
                        v
                          ? [...(root.book_ids || []), book.id]
                          : root.book_ids.filter((id) => id !== book.id),
                      )
                    }
                  />
                ))}
              </div>
            </details>
            <details>
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
            question={{
              ...q,
              stem: "",
              options: [],
              figure_asset_ids: [],
              figures: [],
              rendered_figures: [],
              materials: [],
              parts: [],
            }}
            explanation
            knowledge
          />
        </details>
      )}
      <div className="editor-footer">
        <div>
          {root.id && (
            <Button
              kind="danger"
              onClick={async () => {
                if (!window.confirm("确定删除这道错题吗？")) return;
                try {
                  await remove(`/questions/${root.id}`);
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
          <Button
            busy={busy}
            disabled={tasks.blocked}
            onClick={() => void handleSave(true)}
          >
            确认全部答案
          </Button>
          <Button
            busy={busy}
            disabled={tasks.blocked}
            onClick={() => void handleSave()}
          >
            保存
          </Button>
          <Button
            kind="primary"
            disabled={!root.answer_confirmed || dirty || tasks.blocked}
            onClick={() => setSchedule("explain")}
          >
            {tasks.active.length ? "已有处理任务" : "生成讲解"}
          </Button>
        </div>
      </div>
      {schedule && (
        <ScheduleDialog
          title={
            schedule === "extract"
              ? "AI 整理题目"
              : schedule === "audio"
                ? "补全听力资料"
                : "生成讲解"
          }
          onClose={() => setSchedule(null)}
          onSubmit={async (timing) => {
            const saved = dirty || !root.id ? await save() : root;
            return post<Job>(`/questions/${saved.id}/${schedule}`, timing);
          }}
        />
      )}
    </Modal>
  );
}
