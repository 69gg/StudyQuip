import { useEffect, useRef, useState } from "react";
import { post } from "./api";
import { MathText } from "./Content";
import type { Book, Job, Subject } from "./types";
import { Button, Check, Field, useNotice, useRemote } from "./ui";
import { useJobs, isActiveJob, JobProgress } from "./JobProgress";
type SearchHit = {
  id?: string;
  block_id?: string;
  text?: string;
  content?: string;
  book_title?: string;
  node_path?: string[];
  path?: string[];
  score?: number;
  routes?: string[];
  retrieval_routes?: string[];
};
export default function Search({
  subjects,
  books,
}: {
  subjects: Subject[];
  books: Book[];
}) {
  const [query, setQuery] = useState(""),
    [subject, setSubject] = useState(""),
    [selected, setSelected] = useState<string[]>([]),
    [mode, setMode] = useState("hybrid"),
    [keyword, setKeyword] = useState("any"),
    [node, setNode] = useState(""),
    [bookQuery, setBookQuery] = useState(""),
    [submitted, setSubmitted] = useState(""),
    [busy, setBusy] = useState(false),
    [results, setResults] = useState<SearchHit[] | null>(null),
    [job, setJob] = useState<Job | null>(null);
  const nodes = useRemote<{ id: string; title: string; parent_id?: string }[]>(
    selected.length === 1 ? `/books/${selected[0]}/nodes` : null,
    [],
  );
  const notice = useNotice();
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
  useEffect(() => setNode(""), [selected.join(",")]);
  const tasks = useJobs();
  const restored = useRef(false);
  useEffect(() => {
    if (!tasks.ready) return;
    const current = job
      ? tasks.jobs.find((item) => item.id === job.id)
      : !restored.current
        ? tasks.jobs.find((item) => item.kind === "search")
        : undefined;
    if (!restored.current && current?.input) {
      setQuery(String(current.input.query || ""));
      setSubmitted(String(current.input.query || ""));
      setSubject(String(current.input.subject_id || ""));
      setSelected((current.input.book_ids || []) as string[]);
      setMode(String(current.input.mode || "hybrid"));
      setKeyword(String(current.input.keyword_mode || "any"));
    }
    restored.current = true;
    if (!current) return;
    setJob(current);
    if (current.status === "completed")
      setResults((current.result as { hits?: SearchHit[] })?.hits || []);
  }, [tasks.jobs, tasks.ready, job?.id]);
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>检索教材</h1>
          <p>沿着教材目录，找到相关知识和原文。</p>
        </div>
      </div>
      <form
        className="search-form stack"
        onSubmit={async (e) => {
          e.preventDefault();
          if (!query.trim()) return;
          setSubmitted(query.trim());
          setBusy(true);
          setJob(null);
          setResults(null);
          try {
            const value = await post<
              | SearchHit[]
              | { results?: SearchHit[]; hits?: SearchHit[]; job?: Job }
            >("/search", {
              query: query.trim(),
              node_id: node || undefined,
              subject_id: subject || undefined,
              book_ids: selected,
              mode,
              keyword_mode: keyword,
              limit: 12,
            });
            if (!Array.isArray(value) && value.job) setJob(value.job);
            else
              setResults(
                Array.isArray(value)
                  ? value
                  : value.results || value.hits || [],
              );
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
            onChange={(e) => setQuery(e.target.value)}
            placeholder="输入知识点、公式或问题…"
          />
          <Button
            kind="primary"
            type="submit"
            busy={busy}
            disabled={
              !tasks.ready || !!tasks.error || (!!job && isActiveJob(job))
            }
          >
            {job && isActiveJob(job) ? "检索处理中" : "检索"}
          </Button>
        </div>
        <div className="form-grid">
          <Field label="科目">
            <select
              value={subject}
              onChange={(e) => {
                setSubject(e.target.value);
                setSelected([]);
              }}
            >
              <option value="">全部科目</option>
              {subjects.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <details>
          <summary>
            限定教材范围
            {selected.length ? ` · 已选 ${selected.length} 本` : " · 默认全部"}
          </summary>
          <div className="inline-actions">
            <input
              aria-label="筛选教材"
              placeholder="按书名筛选…"
              value={bookQuery}
              onChange={(event) => setBookQuery(event.target.value)}
            />
            <Button disabled={!selected.length} onClick={() => setSelected([])}>
              清空选择
            </Button>
          </div>
          <div className="book-choices">
            {books
              .filter(
                (b) =>
                  (!subject || b.subject_id === subject) &&
                  b.title.toLowerCase().includes(bookQuery.toLowerCase()),
              )
              .map((book) => (
                <Check
                  key={book.id}
                  label={book.title}
                  checked={selected.includes(book.id)}
                  onChange={(v) =>
                    setSelected(
                      v
                        ? [...selected, book.id]
                        : selected.filter((id) => id !== book.id),
                    )
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
              {nodes.error && (
                <small role="alert">目录读取失败：{nodes.error}</small>
              )}
            </Field>
          )}
          <p className="hint">
            不选教材时检索所选科目的全部教材。选择一本后可进一步限定任意目录及其子树。
          </p>
        </details>
        <details>
          <summary>
            检索方式 ·{" "}
            {
              (
                {
                  hybrid: "结构与混合",
                  keyword: "关键词",
                  phrase: "连续原文",
                  semantic: "语义",
                } as Record<string, string>
              )[mode]
            }
          </summary>
          <div className="form-grid">
            {" "}
            <Field label="检索方式">
              <select value={mode} onChange={(e) => setMode(e.target.value)}>
                <option value="hybrid">结构与混合检索</option>
                <option value="keyword">关键词检索</option>
                <option value="phrase">连续原文检索</option>
                <option value="semantic">语义检索</option>
              </select>
            </Field>
            {["hybrid", "keyword"].includes(mode) && (
              <Field label="关键词匹配">
                <select
                  value={keyword}
                  onChange={(e) => setKeyword(e.target.value)}
                >
                  <option value="any">包含任意关键词</option>
                  <option value="all">包含全部关键词</option>
                </select>
              </Field>
            )}
          </div>
          <p className="hint">
            混合检索结合目录、关键词和向量；连续原文按完整短语查找。语义检索需要先配置嵌入模型并建立教材索引。
          </p>
        </details>
      </form>
      {tasks.error && (
        <p className="inline-error">暂时无法读取任务状态，请稍后重试。</p>
      )}
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
              没有找到相关内容。可以调整词语、检索方式或教材范围。
            </div>
          )}
          {results.map((hit, i) => (
            <article className="search-hit" key={hit.block_id || hit.id || i}>
              <div className="record-meta">
                <strong>{hit.book_title || "教材原文"}</strong>
                <span>{(hit.node_path || hit.path || []).join(" › ")}</span>
              </div>
              <MathText text={hit.text || hit.content || ""} />
            </article>
          ))}
        </div>
      )}
    </>
  );
}
