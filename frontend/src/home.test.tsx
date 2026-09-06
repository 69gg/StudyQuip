import { describe, expect, it } from "vitest";
import {
  hasActiveTasks,
  homeTasks,
  questionState,
  recentItems,
  setupSteps,
} from "./home-model";
import type { Book, Job, Model, Question, Subject } from "./types";

describe("首页的实际数据与待处理状态", () => {
  it("按最后更新时间取最近记录，不修改源列表或为缺失时间编造日期", () => {
    const items = [
      { id: "no-date", revision: 1 },
      { id: "old", revision: 1, created_at: 100 },
      { id: "new", revision: 1, updated_at: "2026-09-06T12:00:00Z" },
    ];
    expect(recentItems(items, 2).map((item) => item.id)).toEqual([
      "new",
      "old",
    ]);
    expect(items[0].id).toBe("no-date");
    expect(recentItems([], 6)).toEqual([]);
  });

  it("优先显示需人工处理与失败任务，等待时段保持自己的状态，完成项不占位置", () => {
    const jobs = [
      "completed",
      "running",
      "waiting_window",
      "failed",
      "waiting_review",
      "cancelled",
    ].map((status, index): Job => ({
      id: String(index),
      revision: 1,
      resource_id: "q",
      kind: "question_explain",
      status,
      created_at: index,
    }));
    expect(homeTasks(jobs).map((job) => job.status)).toEqual([
      "waiting_review",
      "failed",
      "running",
    ]);
    expect(homeTasks(jobs, 8).map((job) => job.status)).toEqual([
      "waiting_review",
      "failed",
      "running",
      "waiting_window",
    ]);
    expect(hasActiveTasks(jobs)).toBe(true);
    expect(
      hasActiveTasks(
        jobs.filter((job) =>
          ["failed", "waiting_review", "completed"].includes(job.status),
        ),
      ),
    ).toBe(false);
  });

  it("只依据确认与讲解状态给出下一步，不把已有模型当作连接成功", () => {
    const base: Question = {
      id: "question",
      revision: 1,
      subject_id: "subject",
      type: "short_answer",
      stem: "请说明解题过程。",
      options: [],
      answer: "参考答案",
      answer_confirmed: false,
      notes: "",
      reference_text: "",
      asset_ids: [],
      reference_asset_ids: [],
      figure_asset_ids: [],
      book_ids: [],
      explanation: { summary: "旧讲解" },
    };
    expect(questionState(base).label).toBe("待确认答案");
    expect(
      questionState({
        ...base,
        answer_confirmed: true,
        explanation_stale: true,
      }).label,
    ).toBe("讲解待更新");
    expect(questionState({ ...base, answer_confirmed: true }).label).toBe(
      "已有讲解",
    );
    expect(
      questionState({ ...base, answer_confirmed: true, explanation: {} }).label,
    ).toBe("可生成讲解");
    expect(setupSteps([], [], []).map((step) => step.href)).toEqual([
      "#settings",
      "#books?new=1",
      "#questions?new=1",
    ]);
    const vision: Model = {
      id: "vision",
      revision: 1,
      name: "识别模型",
      role: "vision",
      protocol: "chat",
      base_url: "https://models.example.invalid/v1",
      model: "example-model",
      thinking: "omit",
      max_output_tokens: 4096,
      max_tokens_field: "max_completion_tokens",
      context_tokens: 24000,
      timeout_seconds: 60,
      retries: 2,
      max_tool_rounds: 8,
      max_concurrency: 4,
      store: false,
      strict_tools: false,
      extra_body: {},
      windows: [],
      timezone: "Asia/Shanghai",
    };
    const configured: Model[] = [
      vision,
      { ...vision, id: "chat", role: "chat" },
    ];
    const subjects: Subject[] = [{ id: "subject", revision: 1, name: "数学" }];
    const books: Book[] = [
      {
        id: "book",
        revision: 1,
        subject_id: "subject",
        title: "教材",
        asset_ids: [],
      },
    ];
    expect(
      setupSteps(subjects, books, configured).map((step) => step.href),
    ).toEqual(["#questions?new=1"]);
  });
});
