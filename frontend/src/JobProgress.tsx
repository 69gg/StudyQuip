import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api, post } from "./api";
import { jobKindLabels, type Job, type RequestActivity } from "./types";
import { Badge, Button, ScheduleDialog, formatTime } from "./ui";

const POLL_INTERVAL_MS = 3000;
export const activeStatuses = new Set([
  "queued",
  "running",
  "waiting_window",
  "waiting_review",
]);
export const isActiveJob = (job: Job) => activeStatuses.has(job.status);
export function matchingJobs(
  jobs: Job[],
  resourceId: string,
  kinds?: string[],
  includeChildren = false,
) {
  return jobs.filter(
    (job) =>
      (job.resource_id === resourceId ||
        (includeChildren && job.book_id === resourceId)) &&
      (!kinds || kinds.includes(job.kind)),
  );
}
type JobState = {
  jobs: Job[];
  ready: boolean;
  error: string;
  reload: () => void;
};
const JobContext = createContext<JobState>({
  jobs: [],
  ready: false,
  error: "",
  reload: () => {},
});
export function JobsProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<Omit<JobState, "reload">>({
    jobs: [],
    ready: false,
    error: "",
  });
  const reloadRef = useRef<() => void>(() => {});
  const reload = useCallback(() => reloadRef.current(), []);
  useEffect(() => {
    let disposed = false;
    let controller: AbortController | undefined;
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      clearTimeout(timer);
      controller?.abort();
      const current = new AbortController();
      controller = current;
      try {
        const jobs = await api<Job[]>("/jobs", { signal: current.signal });
        if (!disposed && !current.signal.aborted)
          setState({ jobs, ready: true, error: "" });
      } catch (error) {
        if (!disposed && !current.signal.aborted)
          setState((old) => ({ ...old, error: (error as Error).message }));
      } finally {
        if (!disposed && !current.signal.aborted)
          timer = setTimeout(refresh, POLL_INTERVAL_MS);
      }
    }
    function changed(event: Event) {
      const job = (event as CustomEvent<Job | undefined>).detail;
      if (job?.id && job.kind && job.status)
        setState((old) => ({
          ...old,
          jobs: [
            { ...old.jobs.find((item) => item.id === job.id), ...job },
            ...old.jobs.filter((item) => item.id !== job.id),
          ],
        }));
      void refresh();
    }
    reloadRef.current = () => void refresh();
    window.addEventListener("studyquip:jobs-changed", changed);
    window.addEventListener("focus", reload);
    void refresh();
    return () => {
      disposed = true;
      clearTimeout(timer);
      controller?.abort();
      reloadRef.current = () => {};
      window.removeEventListener("studyquip:jobs-changed", changed);
      window.removeEventListener("focus", reload);
    };
  }, [reload]);
  return (
    <JobContext.Provider value={{ ...state, reload }}>
      {children}
    </JobContext.Provider>
  );
}
export const useJobs = () => useContext(JobContext);
export function ResumeJobButton({ job }: { job: Job }) {
  const [open, setOpen] = useState(false);
  if (!job.resume?.available) return null;
  const continuing =
    job.resume.has_saved_progress || job.status === "waiting_review";
  const label = continuing ? "继续处理" : "重新执行";
  return (
    <>
      <Button kind="primary" onClick={() => setOpen(true)}>
        {label}
      </Button>
      {open && (
        <ScheduleDialog
          title={label}
          description={
            continuing
              ? "保留已完成内容，从未完成阶段继续。"
              : "此任务还没有可复用的处理结果，将重新尝试。"
          }
          onClose={() => setOpen(false)}
          onSubmit={(timing) => post(`/jobs/${job.id}/resume`, timing)}
        />
      )}
    </>
  );
}
export function useJobCompletion(jobs: Job[], onComplete: () => void) {
  const previous = useRef<Map<string, string>>(new Map());
  const callback = useRef(onComplete);
  callback.current = onComplete;
  useEffect(() => {
    const finished = jobs.some(
      (job) =>
        previous.current.has(job.id) &&
        activeStatuses.has(previous.current.get(job.id)!) &&
        !isActiveJob(job),
    );
    previous.current = new Map(jobs.map((job) => [job.id, job.status]));
    if (finished) callback.current();
  }, [jobs]);
}
export function useResourceJobs(
  resourceId: string,
  kinds?: string[],
  includeChildren = false,
) {
  const state = useJobs();
  const jobs = matchingJobs(state.jobs, resourceId, kinds, includeChildren);
  return {
    ...state,
    jobs,
    active: jobs.filter(isActiveJob),
    blocked: !state.ready || !!state.error || jobs.some(isActiveJob),
  };
}
const toolLabels: Record<string, string> = {
  submit_result: "提交整理结果",
  read_block: "读取教材正文",
  browse_textbook: "查看教材目录与概念",
  search_textbook: "检索教材",
};
function RequestProgress({ request: r }: { request: RequestActivity }) {
  const now = Date.now() / 1000;
  const state =
    r.state === "requesting"
      ? (
          {
            thinking: "模型正在思考",
            arguments: "正在生成工具参数",
            content: "正在接收内容",
          } as Record<string, string>
        )[r.stream_phase || ""] ||
        (r.streaming ? "等待流式内容" : "等待模型响应")
      : (
          {
            waiting_capacity: "等待并发名额",
            retrying: "等待重试",
            tools: "正在执行工具",
            failed: "请求失败",
          } as Record<string, string>
        )[r.state] || r.state;
  return (
    <div className="request-detail">
      <p>
        {r.page ? `原页 ${r.page} · ` : ""}
        {state}
        {r.state === "tools" && r.tool_name
          ? `：${toolLabels[r.tool_name] || r.tool_name}`
          : ""}
        {typeof r.rounds === "number"
          ? ` · 第 ${r.rounds + (r.state === "tools" ? 0 : 1)} 轮`
          : ""}
        {r.at ? ` · ${Math.max(0, Math.floor(now - r.at))} 秒` : ""}
      </p>
      <p className="hint">
        {r.model}（配置版本 {r.revision}）
        {r.attempt ? ` · 本轮第 ${r.attempt} 次请求` : ""}
        {r.attempt && r.attempt > 1
          ? `（网络重试 ${r.attempt - 1}/${r.retry_limit ?? "—"}）`
          : ""}
        {r.format_attempt
          ? ` · 结果格式修复 ${r.format_attempt}/${r.format_limit}`
          : ""}
        {r.state === "retrying" && r.next_at
          ? ` · ${Math.max(0, Math.ceil(r.next_at - now))} 秒后重试`
          : ""}
      </p>
      {r.last_failure && (
        <p className="hint">上次请求：{r.last_failure.reason}</p>
      )}
      {r.state === "retrying" &&
        r.reason &&
        r.reason !== r.last_failure?.reason && (
          <p className="hint">{r.reason}</p>
        )}
      {r.streaming && r.state === "requesting" && (
        <p className="stream-progress">
          {r.first_received_at ? (
            <>
              已接收 {r.received_events || 0} 个事件 · 正文{" "}
              {r.output_characters || 0} 字符 · 思考{" "}
              {r.reasoning_characters || 0} 字符 · 工具参数{" "}
              {r.tool_argument_characters || 0} 字符
              {r.at
                ? ` · 首次接收 ${Math.max(0, Math.round(r.first_received_at - r.at))} 秒`
                : ""}
              {r.last_received_at
                ? ` · 距上次接收 ${Math.max(0, Math.floor(now - r.last_received_at))} 秒`
                : ""}
              {!!r.tool_names?.length &&
                ` · 工具：${r.tool_names.map((name) => toolLabels[name] || name).join("、")}`}
            </>
          ) : (
            "等待首个流式事件"
          )}
        </p>
      )}
    </div>
  );
}
export function JobProgress({
  job,
  detailed = false,
}: {
  job: Job;
  detailed?: boolean;
}) {
  const p = job.progress;
  const working = job.status === "running" && !job.recovering;
  const requests = [
    ...(p?.active_requests || []),
    ...(p?.embedding_activity ? [p.embedding_activity] : []),
  ];
  return (
    <div className="job-progress">
      {(!detailed || (p?.phase && p.phase !== jobKindLabels[job.kind])) && (
        <div className="row-between">
          <strong>{p?.phase || jobKindLabels[job.kind]}</strong>
          {!detailed && <Badge status={job.status} />}
        </div>
      )}
      {!!p?.total_pages && (
        <>
          <div className="progress-meters">
            <label>
              原页识别{" "}
              <span>
                {p.recognized_pages} / {p.total_pages} 页
              </span>
              <progress
                aria-label="原页识别进度"
                value={p.recognized_pages}
                max={p.total_pages}
              />
            </label>
            <label>
              正式整理{" "}
              <span>
                {p.processed_pages} / {p.total_pages} 页
              </span>
              <progress
                aria-label="正式整理进度"
                value={p.processed_pages}
                max={p.total_pages}
              />
            </label>
          </div>
          <p className="hint">
            待校对 {p.review_pages} 页 · 已跳过 {p.skipped_pages} 页 · 正文{" "}
            {p.blocks} 块 · 目录概述 {p.summarized_nodes} / {p.nodes}
          </p>
        </>
      )}
      {typeof p?.embedding_total === "number" && (
        <label className="embedding-progress">
          向量索引 {p.embedding_completed} / {p.embedding_total} 项
          {p.embedding_total > 0 && (
            <progress
              aria-label="向量索引进度"
              value={p.embedding_completed}
              max={p.embedding_total}
            />
          )}
        </label>
      )}
      {p?.current_page && (
        <p className="hint">当前正式整理：第 {p.current_page} 个原页</p>
      )}
      {working && requests.length > 0 && (
        <p className="hint">
          模型请求：{requests.filter((r) => r.state === "requesting").length}{" "}
          个进行中，
          {requests.filter((r) => r.state === "waiting_capacity").length}{" "}
          个等待名额，{requests.filter((r) => r.state === "retrying").length}{" "}
          个等待重试
        </p>
      )}
      {working &&
        (detailed ? requests : requests.slice(0, 1)).map((request, index) => (
          <RequestProgress key={index} request={request} />
        ))}
      {!!p?.enrichment?.rejected && (
        <p className="hint" role="status">
          已保存正文；{p.enrichment.rejected}{" "}
          条概念或关系未通过校验，已单独记录，不影响继续整理。
        </p>
      )}
      {job.not_before &&
      job.not_before > Date.now() / 1000 &&
      isActiveJob(job) ? (
        <p className="hint">最早执行：{formatTime(job.not_before)}</p>
      ) : null}
      {job.waiting_reason && (
        <p className="inline-error">{job.waiting_reason}</p>
      )}
      {!!job.error && (
        <p className={job.status === "failed" ? "inline-error" : "hint"}>
          {typeof job.error === "string"
            ? job.error
            : JSON.stringify(job.error)}
        </p>
      )}
      {job.resume?.available && job.resume.has_saved_progress && (
        <p className="hint" role="status">
          已保存处理进度，可以继续处理，无需从头开始。
        </p>
      )}
      {job.resume?.reason && <p className="hint">{job.resume.reason}</p>}
      {detailed && (
        <>
          <p className="hint">
            执行尝试 {job.attempts || 0} 次 · 已完成
            {job.kind === "book_process"
              ? `正文单元 ${p?.completed_units || 0}`
              : `处理阶段 ${p?.completed_stages || 0}`}
          </p>
          <p className="hint">
            已记录用量：{p?.usage?.requests || 0} 次返回 · 输入{" "}
            {p?.usage?.input_tokens || 0} tokens · 输出{" "}
            {p?.usage?.output_tokens || 0} tokens
          </p>
          {!!p?.tool_events?.length && (
            <details className="task-activity">
              <summary>
                最近工具活动
                {p.tool_errors ? ` · ${p.tool_errors} 次调用需调整` : ""}
              </summary>
              <ol>
                {p.tool_events.map((event, index) => (
                  <li key={index}>
                    <span>
                      {event.page ? `原页 ${event.page} · ` : ""}第{" "}
                      {event.round} 轮 · {toolLabels[event.name] || event.name}
                    </span>
                    <span className="hint">
                      {event.status === "error" ? "需调整调用" : "已完成"} ·{" "}
                      {event.duration_seconds} 秒 · {formatTime(event.at)}
                    </span>
                    {event.reason && <p className="hint">{event.reason}</p>}
                  </li>
                ))}
              </ol>
            </details>
          )}
          {p?.last_activity_at && (
            <p className="hint">
              最近处理活动：{formatTime(p.last_activity_at)}
              {working ? " · 任务租约有效" : ""}
            </p>
          )}
        </>
      )}
    </div>
  );
}
export function ResourceProgress({
  resourceId,
  kinds,
  includeChildren = false,
}: {
  resourceId: string;
  kinds?: string[];
  includeChildren?: boolean;
}) {
  const state = useResourceJobs(resourceId, kinds, includeChildren);
  if (!state.ready || state.error)
    return (
      <p className="hint" role="status">
        {state.error
          ? "暂时无法获取任务状态，已暂停重复提交入口。"
          : "正在读取处理进度…"}
      </p>
    );
  const job = state.active[0] || state.jobs[0];
  if (!job) return null;
  return (
    <section className="resource-progress">
      <JobProgress job={job} />
      <a href={`#tasks?job=${encodeURIComponent(job.id)}`}>
        查看任务详情
        {state.active.length > 1 ? ` · ${state.active.length} 个任务` : ""}
      </a>
    </section>
  );
}
