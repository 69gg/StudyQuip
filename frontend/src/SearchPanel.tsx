import { useEffect, useRef, useState } from "react";
import { ArrowDownUp, Plus, X } from "lucide-react";
import { post } from "./api";
import { MathText } from "./Content";
import {
  questionTypes,
  type Book,
  type Job,
  type Subject,
  type QuestionType,
} from "./types";
import { Button, Check, Field, useNotice, useRemote } from "./ui";
import { useJobs, isActiveJob, JobProgress } from "./JobProgress";

export type SearchMethod = "keyword" | "semantic";
export const methodLabels: Record<SearchMethod, string> = {
  keyword: "关键词",
  semantic: "向量",
};
export type SearchHit = {
  id: string;
  text: string;
  book_title?: string;
  path?: string[];
  method?: SearchMethod;
  rank?: number;
  step?: number;
  question_id?: string;
  question_title?: string;
  question_type?: QuestionType;
  subject_id?: string;
  question_revision?: number;
  part?: string;
  part_label?: string;
  answer_confirmed?: boolean;
};
type Options = {
  parts: Record<string, string>;
  default_limit: number;
  max_limit: number;
  default_methods: SearchMethod[];
};

export default function SearchPanel({
  subjects,
  books = [],
  questionOnly = false,
  initialQuery = "",
  selectedIds = [],
  onSelect,
}: {
  subjects: Subject[];
  books?: Book[];
  questionOnly?: boolean;
  initialQuery?: string;
  selectedIds?: string[];
  onSelect?: (hit: SearchHit) => void;
}) {
  const context = questionOnly ? "print" : "search";
  const [target, setTarget] = useState(questionOnly ? "question" : "book"),
    [query, setQuery] = useState(initialQuery),
    [subject, setSubject] = useState(""),
    [selected, setSelected] = useState<string[]>([]),
    [parts, setParts] = useState(["stem"]),
    [types, setTypes] = useState<string[]>([]),
    [keyword, setKeyword] = useState("any"),
    [node, setNode] = useState(""),
    [bookQuery, setBookQuery] = useState(""),
    [submitted, setSubmitted] = useState(""),
    [busy, setBusy] = useState(false),
    [results, setResults] = useState<SearchHit[] | null>(null),
    [job, setJob] = useState<Job | null>(null),
    [limit, setLimit] = useState<number | null>(null),
    [methods, setMethods] = useState<SearchMethod[]>(() => {
      try {
        const value: unknown = JSON.parse(
          localStorage.getItem("studyquip.search.methods") || "null",
        );
        if (
          Array.isArray(value) &&
          value.length >= 1 &&
          value.length <= 2 &&
          new Set(value).size === value.length &&
          value.every((v) => v === "keyword" || v === "semantic")
        )
          return value;
      } catch {
        /* A malformed saved preference falls back to one method. */
      }
      return ["keyword"];
    });
  const options = useRemote<Options | null>("/search/options", null);
  const nodes = useRemote<{ id: string; title: string; parent_id?: string }[]>(
    target === "book" && selected.length === 1
      ? `/books/${selected[0]}/nodes`
      : null,
    [],
  );
  const notice = useNotice(),
    tasks = useJobs(),
    restored = useRef(false);
  const resultLimit = limit ?? options.data?.default_limit;
  useEffect(() => {
    localStorage.setItem("studyquip.search.methods", JSON.stringify(methods));
  }, [methods]);
  useEffect(() => setNode(""), [selected.join(",")]);
  useEffect(() => {
    if (!tasks.ready) return;
    const current = job
      ? tasks.jobs.find((item) => item.id === job.id)
      : !restored.current
        ? tasks.jobs.find(
            (item) =>
              item.kind === "search" &&
              item.input?.mode !== "hybrid" &&
              (item.input?.context || "search") === context,
          )
        : undefined;
    if (!restored.current && current?.input) {
      const input = current.input;
      setQuery(String(input.query || ""));
      setSubmitted(String(input.query || ""));
      setTarget(questionOnly ? "question" : String(input.target || "book"));
      setSubject(String(input.subject_id || ""));
      setSelected((input.book_ids || []) as string[]);
      setParts((input.parts || ["stem"]) as string[]);
      setTypes((input.question_types || []) as string[]);
      const saved = input.methods as SearchMethod[] | undefined;
      setMethods(
        saved?.length && saved.every((m) => m in methodLabels)
          ? saved
          : [input.mode === "semantic" ? "semantic" : "keyword"],
      );
      setKeyword(
        input.mode === "phrase"
          ? "phrase"
          : String(input.keyword_mode || "any"),
      );
      setLimit(typeof input.limit === "number" ? input.limit : null);
      setNode(String(input.node_id || ""));
    }
    restored.current = true;
    if (!current) return;
    setJob(current);
    if (current.status === "completed")
      setResults((current.result as { hits?: SearchHit[] })?.hits || []);
  }, [tasks.jobs, tasks.ready, job?.id, context, questionOnly]);
  function nodeLabel(id: string): string {
    const chain: string[] = [],
      seen = new Set<string>();
    let next: string | undefined = id;
    while (next && !seen.has(next)) {
      seen.add(next);
      const item = nodes.data.find((candidate) => candidate.id === next);
      if (!item) break;
      chain.unshift(item.title);
      next = item.parent_id;
    }
    return chain.join(" › ");
  }
  function toggle(values: string[], id: string, checked: boolean): string[] {
    return checked ? [...values, id] : values.filter((item) => item !== id);
  }
  return (
    <div className="search-panel">
      {!questionOnly && (
        <div className="settings-tabs" role="tablist" aria-label="检索范围">
          {[
            ["book", "教材"],
            ["question", "错题"],
          ].map(([id, label]) => (
            <button
              key={id}
              role="tab"
              aria-selected={target === id}
              disabled={!!(job && isActiveJob(job))}
              onClick={() => {
                setTarget(id);
                setResults(null);
                setJob(null);
                restored.current = true;
              }}
            >
              {label}
            </button>
          ))}
        </div>
      )}
      <form
        className="search-form stack"
        onSubmit={async (event) => {
          event.preventDefault();
          if (!query.trim()) return;
          setBusy(true);
          setJob(null);
          setResults(null);
          restored.current = true;
          setSubmitted(query.trim());
          try {
            const value = await post<SearchHit[] | { job: Job }>("/search", {
              query: query.trim(),
              target,
              context,
              subject_id: subject || null,
              book_ids: target === "book" ? selected : null,
              node_id: target === "book" ? node || null : null,
              methods,
              keyword_mode: keyword,
              parts,
              question_types: types,
              confirmed_only: questionOnly,
              limit: resultLimit,
            });
            if (Array.isArray(value)) {
              setJob(null);
              setResults(value);
            } else setJob(value.job);
          } catch (error) {
            notice((error as Error).message, true);
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="search-query">
          <input
            required
            aria-label="检索内容"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={
              target === "question"
                ? "输入题目描述、解题思路或知识点…"
                : "输入知识点、公式或问题…"
            }
          />
          <Button
            kind="primary"
            type="submit"
            busy={busy}
            disabled={
              !tasks.ready ||
              !!tasks.error ||
              !!(job && isActiveJob(job)) ||
              !parts.length ||
              !resultLimit
            }
          >
            {job && isActiveJob(job) ? "检索处理中" : "检索"}
          </Button>
        </div>
        <div className="form-grid">
          <Field label="科目">
            <select
              value={subject}
              onChange={(event) => {
                setSubject(event.target.value);
                setSelected([]);
              }}
            >
              <option value="">全部科目</option>
              {subjects.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="每种检索最多返回">
            <input
              type="number"
              required
              min={1}
              max={options.data?.max_limit}
              value={resultLimit ?? ""}
              onChange={(event) => setLimit(Number(event.target.value))}
            />
          </Field>
        </div>
        {target === "question" ? (
          <>
            <fieldset className="search-parts">
              <legend>检索题目部分</legend>
              <div className="check-row">
                {Object.entries(options.data?.parts || {}).map(
                  ([id, label]) => (
                    <Check
                      key={id}
                      label={label}
                      checked={parts.includes(id)}
                      onChange={(checked) =>
                        setParts(toggle(parts, id, checked))
                      }
                    />
                  ),
                )}
              </div>
              <small className="hint">
                可选一项或多项；每道题只保留最相关的命中部分。解析过期后不参与检索。
              </small>
            </fieldset>
            <details>
              <summary>
                限定题型
                {types.length ? ` · 已选 ${types.length} 种` : " · 全部"}
              </summary>
              <div className="check-row">
                {Object.entries(questionTypes).map(([id, label]) => (
                  <Check
                    key={id}
                    label={label}
                    checked={types.includes(id)}
                    onChange={(checked) => setTypes(toggle(types, id, checked))}
                  />
                ))}
              </div>
              <p className="hint">
                科目、题型和所选部分同时生效；大题中的小题也可按自身题型命中。
              </p>
            </details>
          </>
        ) : (
          <details>
            <summary>
              限定教材范围
              {selected.length
                ? ` · 已选 ${selected.length} 本`
                : " · 默认全部"}
            </summary>
            <div className="inline-actions">
              <input
                aria-label="筛选教材"
                placeholder="按书名筛选…"
                value={bookQuery}
                onChange={(event) => setBookQuery(event.target.value)}
              />
              <Button
                disabled={!selected.length}
                onClick={() => setSelected([])}
              >
                清空选择
              </Button>
            </div>
            <div className="book-choices">
              {books
                .filter(
                  (book) =>
                    (!subject || book.subject_id === subject) &&
                    book.title.toLowerCase().includes(bookQuery.toLowerCase()),
                )
                .map((book) => (
                  <Check
                    key={book.id}
                    label={book.title}
                    checked={selected.includes(book.id)}
                    onChange={(checked) =>
                      setSelected(toggle(selected, book.id, checked))
                    }
                  />
                ))}
            </div>
            {selected.length === 1 && (
              <Field label="限定目录（包含全部下级内容）">
                <select
                  disabled={nodes.loading}
                  value={node}
                  onChange={(event) => setNode(event.target.value)}
                >
                  <option value="">整本教材</option>
                  {nodes.data.map((item) => (
                    <option key={item.id} value={item.id}>
                      {nodeLabel(item.id)}
                    </option>
                  ))}
                </select>
              </Field>
            )}
            <p className="hint">
              不选教材时使用同科目全部教材。单本教材可继续限定任意目录子树。
            </p>
          </details>
        )}
        <div className="search-methods">
          {methods.map((method, index) => (
            <div className="search-method" key={index}>
              <Field
                label={
                  methods.length === 1 ? "检索方式" : `第 ${index + 1} 种检索`
                }
              >
                <select
                  value={method}
                  onChange={(event) => {
                    const next = event.target.value as SearchMethod;
                    setMethods(
                      methods.length === 1
                        ? [next]
                        : index === 0
                          ? [next, next === "keyword" ? "semantic" : "keyword"]
                          : [next === "keyword" ? "semantic" : "keyword", next],
                    );
                  }}
                >
                  <option value="keyword">关键词检索</option>
                  <option value="semantic">向量检索</option>
                </select>
              </Field>
              {methods.length > 1 && (
                <Button
                  aria-label={`移除第 ${index + 1} 种检索`}
                  onClick={() =>
                    setMethods(
                      methods.filter((_, position) => index !== position),
                    )
                  }
                >
                  <X size={16} />
                </Button>
              )}
            </div>
          ))}
          {methods.length === 1 ? (
            <Button
              onClick={() =>
                setMethods([
                  ...methods,
                  methods[0] === "keyword" ? "semantic" : "keyword",
                ])
              }
            >
              <Plus size={16} />
              添加下一种检索
            </Button>
          ) : (
            <Button onClick={() => setMethods([...methods].reverse())}>
              <ArrowDownUp size={16} />
              交换顺序
            </Button>
          )}
        </div>
        {methods.includes("keyword") && (
          <Field label="关键词匹配">
            <select
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
            >
              <option value="any">包含任意关键词</option>
              <option value="all">包含全部关键词</option>
              <option value="phrase">连续原文（短语）</option>
            </select>
          </Field>
        )}
        <p className="hint">
          {methods.length > 1
            ? `先${methodLabels[methods[0]]}，再${methodLabels[methods[1]]}；按此顺序追加结果并去重，各自保留原有排序。`
            : `${methodLabels[methods[0]]}独立排序。`}
          {methods.includes("semantic") &&
            " 向量检索需要嵌入模型；题目保存后由后台自动更新索引。"}
        </p>
        {options.error && (
          <p className="inline-error">检索配置读取失败：{options.error}</p>
        )}
      </form>
      {job && (
        <div className="gentle-notice">
          <JobProgress job={job} />
          <a href={`#tasks?job=${encodeURIComponent(job.id)}`}>查看任务详情</a>
        </div>
      )}
      {results && (
        <div className="search-results">
          <div className="list-label">
            “{submitted}” · {results.length} 条相关内容
          </div>
          {!results.length && (
            <div className="empty">
              没有找到相关内容。可以调整词语、范围或检索方式；向量索引进度可在任务页查看。
            </div>
          )}
          {results.map((hit, index) => (
            <article className="search-hit" key={hit.question_id || hit.id}>
              {hit.method !== results[index - 1]?.method && (
                <div className="search-stage-label">
                  {hit.method ? methodLabels[hit.method] : "检索"}结果
                </div>
              )}
              <div className="record-meta">
                <strong>
                  {hit.question_id
                    ? [
                        subjects.find((item) => item.id === hit.subject_id)
                          ?.name,
                        hit.question_type
                          ? questionTypes[hit.question_type]
                          : "错题",
                      ]
                        .filter(Boolean)
                        .join(" · ")
                    : hit.book_title || "教材原文"}
                </strong>
                <span>
                  {hit.question_id
                    ? `${hit.path?.length ? `小题 ${hit.path.join(".")} · ` : ""}${hit.part_label || "题干"}`
                    : hit.path?.join(" › ")}
                </span>
              </div>
              {hit.question_id && hit.part !== "stem" && hit.question_title && (
                <div className="search-hit-context">
                  <MathText text={hit.question_title} />
                </div>
              )}
              <MathText text={hit.text} />
              {hit.question_id && (
                <div className="inline-actions">
                  <a
                    href={`#questions?question=${encodeURIComponent(hit.question_id)}`}
                    target={questionOnly ? "_blank" : undefined}
                    rel={questionOnly ? "noopener" : undefined}
                  >
                    查看题目
                  </a>
                  {onSelect && (
                    <Button
                      disabled={selectedIds.includes(hit.question_id)}
                      onClick={() => onSelect(hit)}
                    >
                      {selectedIds.includes(hit.question_id)
                        ? "已加入"
                        : "加入打印"}
                    </Button>
                  )}
                  {!hit.answer_confirmed && (
                    <span className="muted">答案待确认</span>
                  )}
                </div>
              )}
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
