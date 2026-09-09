import { useState } from "react";
import { api, post } from "./api";
import { questionTitle, type Question, type Subject, type Job } from "./types";
import { Button, Check, Field, Modal, moveItem, useNotice } from "./ui";
import { QuestionContent } from "./Content";
import { useJobs, isActiveJob } from "./JobProgress";
import SearchPanel from "./SearchPanel";
export default function ExportDialog({
  initial,
  subjects,
  onClose,
}: {
  initial: Question[];
  subjects: Subject[];
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
      <details className="print-search">
        <summary>检索相关题并加入打印</summary>
        <SearchPanel
          subjects={subjects}
          questionOnly
          initialQuery={initial[0]?.stem || ""}
          selectedIds={questions.map((question) => question.id)}
          onSelect={async (hit) => {
            try {
              const question = await api<Question>(
                `/questions/${hit.question_id}`,
              );
              setQuestions((current) =>
                current.some((item) => item.id === question.id)
                  ? current
                  : [...current, question],
              );
            } catch (error) {
              notice((error as Error).message, true);
            }
          }}
        />
      </details>
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
                {i + 1}. {questionTitle(q).slice(0, 34)}
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
              <Button
                aria-label={`移除第 ${i + 1} 题`}
                onClick={() =>
                  setQuestions(questions.filter((item) => item.id !== q.id))
                }
              >
                移除
              </Button>
            </div>
          ))}
          <Button
            kind="primary"
            busy={busy}
            disabled={
              !tasks.ready || !!tasks.error || !!pending || !questions.length
            }
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
              practice={mode === "practice"}
              printing
              blankLines={mode === "practice" ? blank : 0}
            />
          ))}
        </div>
      </div>
    </Modal>
  );
}
