import { useState } from "react";
import { post } from "./api";
import { jobKindLabels, type Job } from "./types";
import {
  Badge,
  Button,
  ScheduleDialog,
  State,
  formatTime,
  useNotice,
} from "./ui";
import { JobProgress, ResumeJobButton, useJobs } from "./JobProgress";
export default function Tasks() {
  const tasks = useJobs();
  const remote = {
    data: tasks.jobs,
    loading: !tasks.ready,
    error: tasks.error,
    reload: tasks.reload,
  };
  const [schedule, setSchedule] = useState<{ job: Job; action: string } | null>(
    null,
  );
  const notice = useNotice();
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
          <section
            className={`task-row ${new URLSearchParams(location.hash.split("?")[1]).get("job") === job.id ? "selected-task" : ""}`}
            key={job.id}
          >
            <div className="row-between">
              <h3>{jobKindLabels[job.kind] || "处理任务"}</h3>
              <Badge status={job.status} />
            </div>
            {job.resource_title && (
              <p className="task-resource">{job.resource_title}</p>
            )}
            <div className="record-meta">
              <span>提交于 {formatTime(job.created_at)}</span>
              {job.not_before && job.not_before > Date.now() / 1000 ? (
                <span>最早执行 {formatTime(job.not_before)}</span>
              ) : null}
            </div>
            <JobProgress job={job} detailed />
            <div className="actions">
              <ResumeJobButton job={job} />
              {[
                "queued",
                "deferred",
                "pending",
                "waiting",
                "waiting_window",
                "paused",
              ].includes(job.status) && (
                <Button
                  onClick={() => setSchedule({ job, action: "reschedule" })}
                >
                  调整执行时间
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
          title="调整执行时间"
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
