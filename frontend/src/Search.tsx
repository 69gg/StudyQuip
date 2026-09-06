import { useEffect, useState } from "react";
import { api, post } from "./api";
import { MathText } from "./Content";
import type { Book, Job, Subject } from "./types";
import { Badge, Button, Check, Field, formatTime, useNotice } from "./ui";
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
    [busy, setBusy] = useState(false),
    [results, setResults] = useState<SearchHit[] | null>(null),
    [job, setJob] = useState<Job | null>(null);
  const notice = useNotice();
  useEffect(() => {
    if (
      !job ||
      ["completed", "succeeded", "failed", "cancelled"].includes(job.status)
    )
      return;
    let active = true;
    const timer = window.setInterval(async () => {
      try {
        const list = await api<Job[]>("/jobs");
        const current = list.find((item) => item.id === job.id);
        if (!active || !current) return;
        setJob(current);
        if (["completed", "succeeded"].includes(current.status)) {
          const result = current.result as { hits?: SearchHit[] } | undefined;
          setResults(result?.hits || []);
        }
      } catch (error) {
        if (active) notice((error as Error).message, true);
      }
    }, 2500);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [job?.id, job?.status, notice]);
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
          setBusy(true);
          setJob(null);
          setResults(null);
          try {
            const value = await post<
              | SearchHit[]
              | { results?: SearchHit[]; hits?: SearchHit[]; job?: Job }
            >("/search", {
              query,
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
          <Button kind="primary" type="submit" busy={busy}>
            检索
          </Button>
        </div>
        <div className="form-grid three">
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
        <details>
          <summary>
            限定教材范围{selected.length ? ` · ${selected.length} 本` : ""}
          </summary>
          <div className="book-choices">
            {books
              .filter((b) => !subject || b.subject_id === subject)
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
        </details>
      </form>
      {job && (
        <div className="gentle-notice">
          <div className="row-between">
            <span>语义检索任务</span>
            <Badge status={job.status} />
          </div>
          {job.not_before && job.not_before > Date.now() / 1000 ? (
            <p>最早执行：{formatTime(job.not_before)}</p>
          ) : null}
          {(job.defer_reason || job.waiting_reason) && (
            <p>{job.defer_reason || job.waiting_reason}</p>
          )}
          {job.error ? (
            <p
              className={
                job.status === "waiting_window" ? "hint" : "inline-error"
              }
            >
              {typeof job.error === "string"
                ? job.error
                : JSON.stringify(job.error)}
            </p>
          ) : null}
          <p>检索会按模型时段执行，结果完成后在此显示，也可在任务页查看。</p>
        </div>
      )}
      {results && (
        <div className="search-results">
          <div className="list-label">找到 {results.length} 条相关内容</div>
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
