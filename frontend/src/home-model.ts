import type { Book, Entity, Job, Model, Question, Subject } from "./types";

export function updatedAt(item: Entity): number {
  const value = item.updated_at ?? item.created_at;
  if (typeof value === "number") return value * 1000;
  const parsed = Date.parse(value || "");
  return Number.isFinite(parsed) ? parsed : 0;
}

export function recentItems<T extends Entity>(items: T[], limit: number): T[] {
  return [...items]
    .sort((a, b) => updatedAt(b) - updatedAt(a) || a.id.localeCompare(b.id))
    .slice(0, limit);
}

const taskPriority: Record<string, number> = {
  waiting_review: 0,
  needs_review: 0,
  failed: 1,
  running: 2,
  queued: 3,
  pending: 3,
  deferred: 4,
  waiting: 4,
  waiting_window: 4,
  paused: 4,
};

export function homeTasks(jobs: Job[], limit = 3): Job[] {
  return [...jobs]
    .filter((job) => job.status in taskPriority)
    .sort(
      (a, b) =>
        taskPriority[a.status] - taskPriority[b.status] ||
        updatedAt(b) - updatedAt(a) ||
        a.id.localeCompare(b.id),
    )
    .slice(0, limit);
}

export function hasActiveTasks(jobs: Job[]): boolean {
  return jobs.some((job) =>
    [
      "running",
      "queued",
      "pending",
      "deferred",
      "waiting",
      "waiting_window",
    ].includes(job.status),
  );
}

export function questionState(question: Question): {
  label: string;
  className: string;
} {
  if (!question.answer_confirmed)
    return { label: "待确认答案", className: "home-state-attention" };
  if (question.explanation_stale)
    return { label: "讲解待更新", className: "home-state-attention" };
  if (question.explanation && Object.keys(question.explanation).length > 0)
    return { label: "已有讲解", className: "home-state-ready" };
  return { label: "可生成讲解", className: "" };
}

export type SetupStep = {
  title: string;
  description: string;
  href: string;
  action: string;
};

export function setupSteps(
  subjects: Subject[],
  books: Book[],
  models: Model[],
): SetupStep[] {
  const steps: SetupStep[] = [];
  const hasCoreModels =
    models.some((model) => model.role === "question_vision") &&
    models.some((model) => model.role === "question_text");
  if (!subjects.length || !hasCoreModels) {
    steps.push({
      title: !subjects.length ? "设置科目与模型" : "配置题目图片与文本模型",
      description: !subjects.length
        ? "添加常用科目，再填写模型连接信息。"
        : "配置题目的图片识别与文本讲解，教材模型可单独设置。",
      href: "#settings",
      action: "前往设置",
    });
  }
  if (!books.length) {
    steps.push({
      title: "导入教材",
      description: "上传文字、图片或 PDF，为讲解提供教材依据。也可以稍后补充。",
      href: "#books?new=1",
      action: "导入教材",
    });
  }
  steps.push({
    title: "录入第一道错题",
    description: "拍照或输入题目，确认答案后生成讲解。",
    href: "#questions?new=1",
    action: "录入错题",
  });
  return steps;
}
