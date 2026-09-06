import { useCallback, useEffect } from "react";
import { ArrowRight, BookOpen, Plus, RefreshCw } from "lucide-react";
import { MathText } from "./Content";
import {
  jobKindLabels,
  questionTypes,
  type Book,
  type Model,
  type Question,
  type Subject,
} from "./types";
import { Badge, Button, State, formatTime, useRemote } from "./ui";
import {
  isActiveJob,
  matchingJobs,
  useJobCompletion,
  useJobs,
} from "./JobProgress";
import {
  hasActiveTasks,
  homeTasks,
  questionState,
  recentItems,
  setupSteps,
} from "./home-model";
import "./home.css";

export default function Home({
  subjects,
  books,
  onRefresh,
}: {
  subjects: Subject[];
  books: Book[];
  onRefresh: () => void;
}) {
  const questions = useRemote<Question[]>("/questions", []);
  const taskState = useJobs();
  const jobs = {
    data: taskState.jobs,
    loading: !taskState.ready,
    error: taskState.error,
    reload: taskState.reload,
  };
  const models = useRemote<Model[]>("/models", []);
  const reload = useCallback(() => {
    questions.reload();
    jobs.reload();
    models.reload();
    onRefresh();
  }, [questions.reload, jobs.reload, models.reload, onRefresh]);
  useJobCompletion(jobs.data, reload);
  const active = hasActiveTasks(jobs.data);

  useEffect(() => {
    window.addEventListener("focus", reload);
    return () => window.removeEventListener("focus", reload);
  }, [reload]);
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") reload();
    }, 10000);
    return () => window.clearInterval(timer);
  }, [active, reload]);

  const recentQuestions = recentItems(questions.data, 6);
  const recentBooks = recentItems(books, 3);
  const tasks = homeTasks(jobs.data);
  const awaitingAnswer = questions.data.filter(
    (question) => !question.answer_confirmed,
  ).length;
  const pendingExplanation = questions.data.filter(
    (question) => question.explanation_stale,
  ).length;
  const initialLoading = questions.loading && !questions.data.length;
  const empty = !questions.error && !initialLoading && !questions.data.length;
  const hasAside = tasks.length > 0 || books.length > 0 || !!jobs.error;
  const refreshing = questions.loading || jobs.loading || models.loading;

  return (
    <div className="home-page">
      <header className="page-heading home-heading">
        <div>
          <h1>首页</h1>
          <p>继续整理错题与教材。</p>
        </div>
        <div className="inline-actions home-actions">
          <Button
            className="home-refresh"
            aria-label="刷新首页"
            title="刷新首页"
            disabled={refreshing}
            onClick={reload}
          >
            <RefreshCw size={16} aria-hidden="true" />
          </Button>
          <a className="button" href="#books?new=1">
            <BookOpen size={16} aria-hidden="true" />
            导入教材
          </a>
          <a className="button primary" href="#questions?new=1">
            <Plus size={17} aria-hidden="true" />
            录入错题
          </a>
        </div>
      </header>

      {!initialLoading && !questions.error && (
        <div className="home-overview" aria-label="资料概览">
          <span>
            <strong>{questions.data.length}</strong> 道错题
          </span>
          <span>
            <strong>{books.length}</strong> 本教材
          </span>
          {awaitingAnswer > 0 && <span>{awaitingAnswer} 道待确认答案</span>}
          {pendingExplanation > 0 && (
            <span>{pendingExplanation} 道讲解待更新</span>
          )}
        </div>
      )}

      <div className={`home-columns ${hasAside ? "" : "home-columns-single"}`}>
        <div className="home-primary">
          <State loading={initialLoading} error={questions.error} />
          {empty && (
            <section
              className="home-setup"
              aria-labelledby="home-start-heading"
            >
              <div className="home-section-heading">
                <h2 id="home-start-heading">从第一道错题开始</h2>
              </div>
              {models.loading && !models.data.length ? (
                <State loading />
              ) : models.error ? (
                <>
                  <State error={`无法读取模型配置：${models.error}`} />
                  <a className="text-link" href="#questions?new=1">
                    先录入题目草稿
                  </a>
                </>
              ) : (
                <ol className="home-steps">
                  {setupSteps(subjects, books, models.data).map(
                    (step, index) => (
                      <li key={step.href}>
                        <span className="home-step-number" aria-hidden="true">
                          {index + 1}
                        </span>
                        <div className="home-step-content">
                          <h3>{step.title}</h3>
                          <p>{step.description}</p>
                          <a className="home-text-action" href={step.href}>
                            {step.action}
                            <ArrowRight size={15} aria-hidden="true" />
                          </a>
                        </div>
                      </li>
                    ),
                  )}
                </ol>
              )}
            </section>
          )}

          {recentQuestions.length > 0 && (
            <section aria-labelledby="home-recent-heading">
              <div className="home-section-heading">
                <h2 id="home-recent-heading">最近错题</h2>
                <a className="home-text-action" href="#questions">
                  查看全部
                  <ArrowRight size={15} aria-hidden="true" />
                </a>
              </div>
              <div className="home-question-list">
                {recentQuestions.map((question) => {
                  const state = questionState(question);
                  return (
                    <article className="home-question-row" key={question.id}>
                      <div className="home-question-meta">
                        <span>
                          {subjects.find(
                            (subject) => subject.id === question.subject_id,
                          )?.name || "未设科目"}
                        </span>
                        <span>{questionTypes[question.type]}</span>
                        <time>
                          {formatTime(
                            question.updated_at ?? question.created_at,
                          )}
                        </time>
                      </div>
                      <MathText
                        text={question.stem || "题目草稿（尚未填写题干）"}
                        className="home-question-preview"
                      />
                      <div className="home-question-footer">
                        <span
                          className={`home-question-state ${state.className}`}
                        >
                          {state.label}
                        </span>
                        <a
                          className="home-text-action"
                          href={`#questions?question=${encodeURIComponent(question.id)}`}
                        >
                          打开题目
                          <ArrowRight size={15} aria-hidden="true" />
                        </a>
                      </div>
                    </article>
                  );
                })}
              </div>
            </section>
          )}
        </div>

        {hasAside && (
          <aside className="home-secondary" aria-label="教材与处理任务">
            {(tasks.length > 0 || jobs.error) && (
              <section aria-labelledby="home-tasks-heading">
                <div className="home-section-heading">
                  <h2 id="home-tasks-heading">处理任务</h2>
                  <a className="home-text-action" href="#tasks">
                    查看全部
                    <ArrowRight size={15} aria-hidden="true" />
                  </a>
                </div>
                <State error={jobs.error} />
                <div className="home-task-list">
                  {tasks.map((job) => (
                    <a
                      className="home-task-row"
                      key={job.id}
                      href={`#tasks?job=${encodeURIComponent(job.id)}`}
                    >
                      <span className="home-task-title">
                        {jobKindLabels[job.kind] || "处理任务"}
                      </span>
                      <Badge status={job.status} />
                      {job.progress?.phase && (
                        <p>
                          {job.progress.phase}
                          {job.progress.total_pages
                            ? ` · 已识别 ${job.progress.recognized_pages} / ${job.progress.total_pages} 页`
                            : ""}
                        </p>
                      )}
                      {(job.defer_reason || job.waiting_reason) && (
                        <p>{job.defer_reason || job.waiting_reason}</p>
                      )}
                    </a>
                  ))}
                </div>
              </section>
            )}

            {recentBooks.length > 0 && (
              <section aria-labelledby="home-books-heading">
                <div className="home-section-heading">
                  <h2 id="home-books-heading">最近教材</h2>
                  <a className="home-text-action" href="#books">
                    教材库
                    <ArrowRight size={15} aria-hidden="true" />
                  </a>
                </div>
                <div className="home-book-list">
                  {recentBooks.map((book) => (
                    <a
                      className="home-book-row"
                      href={`#books?book=${encodeURIComponent(book.id)}`}
                      key={book.id}
                    >
                      <BookOpen
                        size={20}
                        strokeWidth={1.7}
                        aria-hidden="true"
                      />
                      <div>
                        <h3>{book.title}</h3>
                        <span>
                          {subjects.find(
                            (subject) => subject.id === book.subject_id,
                          )?.name || "未设科目"}
                        </span>
                      </div>
                      <Badge
                        status={
                          matchingJobs(
                            jobs.data,
                            book.id,
                            undefined,
                            true,
                          ).find(isActiveJob)?.status || book.status
                        }
                      />
                    </a>
                  ))}
                </div>
              </section>
            )}
          </aside>
        )}
      </div>
    </div>
  );
}
