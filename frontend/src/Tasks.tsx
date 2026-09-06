import { useEffect, useState } from "react";
import { post } from "./api";
import { jobKindLabels, type Job } from "./types";
import {
  Badge,
  Button,
  ScheduleDialog,
  State,
  formatTime,
  useNotice,
  useRemote,
} from "./ui";
export default function Tasks() {
  const remote = useRemote<Job[]>("/jobs", []);
  const [schedule, setSchedule] = useState<{ job: Job; action: string } | null>(
    null,
  );
  const notice = useNotice();
  useEffect(() => {
    const timer = window.setInterval(remote.reload, 4000);
    return () => window.clearInterval(timer);
  }, [remote.reload]);
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>任务</h1>
          <p>查看处理进度、执行时段与结果。</p>
        </div>
        <Button onClick={remote.reload}>刷新</Button>
      </div>
      <State
        loading={remote.loading && remote.data.length === 0}
        error={remote.error}
        empty={!remote.loading && !remote.data.length}
      >
        暂时没有处理任务。
      </State>
      <div className="tasks-list">
        {remote.data.map((job) => (
          <section className="task-row" key={job.id}>
            <div className="row-between">
              <h3>{jobKindLabels[job.kind] || "处理任务"}</h3>
              <Badge status={job.status} />
            </div>
            <div className="record-meta">
              <span>提交于 {formatTime(job.created_at)}</span>
              {job.not_before && job.not_before > Date.now() / 1000 ? (
                <span>最早执行 {formatTime(job.not_before)}</span>
              ) : null}
            </div>
            {(job.defer_reason || job.waiting_reason) && (
              <p className="hint">{job.defer_reason || job.waiting_reason}</p>
            )}
            {job.checkpoint && Object.keys(job.checkpoint).length > 0 && (
              <TaskProgress checkpoint={job.checkpoint} />
            )}{" "}
            {!!job.error && (
              <div
                className={
                  job.status === "waiting_window" ? "hint" : "inline-error"
                }
              >
                {typeof job.error === "string"
                  ? job.error
                  : JSON.stringify(job.error)}
              </div>
            )}
            <div className="actions">
              {["failed", "cancelled"].includes(job.status) && (
                <Button onClick={() => setSchedule({ job, action: "retry" })}>
                  重新执行
                </Button>
              )}
              {[
                "queued",
                "deferred",
                "pending",
                "waiting",
                "waiting_review",
                "waiting_window",
                "needs_review",
                "paused",
              ].includes(job.status) && (
                <Button
                  onClick={() => setSchedule({ job, action: "reschedule" })}
                >
                  {job.status === "waiting_review" ||
                  job.status === "needs_review"
                    ? "继续处理"
                    : "调整执行时间"}
                </Button>
              )}
              {!["completed", "succeeded", "failed", "cancelled"].includes(
                job.status,
              ) && (
                <Button
                  onClick={async () => {
                    try {
                      await post(`/jobs/${job.id}/cancel`);
                      remote.reload();
                      notice("已请求取消。");
                    } catch (e) {
                      notice((e as Error).message, true);
                    }
                  }}
                >
                  取消任务
                </Button>
              )}
              {job.kind === "export_pdf" &&
                ["completed", "succeeded"].includes(job.status) && (
                  <a
                    className="button primary"
                    href={`/api/exports/${encodeURIComponent(job.resource_id)}/file`}
                    download
                  >
                    下载 PDF
                  </a>
                )}
              {job.result != null && (
                <details className="task-result">
                  <summary>查看结果</summary>
                  <pre>
                    {typeof job.result === "string"
                      ? job.result
                      : JSON.stringify(job.result, null, 2)}
                  </pre>
                </details>
              )}
            </div>
          </section>
        ))}
      </div>
      {schedule && (
        <ScheduleDialog
          title={schedule.action === "retry" ? "重新执行任务" : "调整执行时间"}
          onClose={() => setSchedule(null)}
          onSubmit={async (timing) => {
            const result = await post(
              `/jobs/${schedule.job.id}/${schedule.action}`,
              timing,
            );
            remote.reload();
            return result;
          }}
        />
      )}
    </>
  );
}
function TaskProgress({ checkpoint }: { checkpoint: Record<string, unknown> }) {
  const current =
    checkpoint.page_index ??
    checkpoint.processed_pages ??
    checkpoint.completed ??
    checkpoint.index;
  const total = checkpoint.total_pages ?? checkpoint.total;
  const stage = checkpoint.phase ?? checkpoint.stage;
  return (
    <div className="task-progress">
      {!!stage && (
        <span>
          {(
            {
              extracting: "提取内容",
              recognizing: "识别原页",
              reconciling: "整理跨页内容",
              embedding: "生成向量",
              indexing: "建立索引",
              rendering: "渲染 PDF",
            } as Record<string, string>
          )[String(stage)] || String(stage)}
        </span>
      )}
      {typeof current === "number" && (
        <span>
          已处理 {current}
          {typeof total === "number" ? ` / ${total}` : ""}
        </span>
      )}
      {typeof current === "number" &&
        typeof total === "number" &&
        total > 0 && <progress value={current} max={total} />}
    </div>
  );
}
