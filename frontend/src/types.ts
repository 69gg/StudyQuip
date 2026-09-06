export type Entity = {
  id: string;
  revision: number;
  created_at?: number | string;
  updated_at?: number | string;
};
export type Subject = Entity & { name: string };
export type Asset = Entity & {
  name: string;
  mime: string;
  url?: string;
  preview_url?: string;
  width?: number;
  height?: number;
};
export type QuestionType =
  "single_choice" | "multiple_choice" | "fill_blank" | "short_answer";
export type Question = Entity & {
  subject_id: string;
  type: QuestionType;
  stem: string;
  options: { id: string; text: string }[];
  answer: unknown;
  answer_confirmed: boolean;
  wrong_answer?: unknown;
  error_reason?: string;
  optimize_error_reason?: boolean;
  notes: string;
  reference_text: string;
  asset_ids: string[];
  reference_asset_ids: string[];
  figure_asset_ids: string[];
  figures?: { id: string; url: string; name?: string }[];
  book_ids: string[];
  status?: string;
  explanation?: Record<string, unknown>;
  explanation_stale?: boolean;
};
export type Book = Entity & {
  title: string;
  subject_id: string;
  text?: string;
  asset_ids: string[];
  status?: string;
  extra_processing_budget?: number | null;
};
export type Job = Entity & {
  kind: string;
  resource_id: string;
  status: string;
  not_before?: number;
  checkpoint?: Record<string, unknown>;
  error?: unknown;
  result?: unknown;
  defer_reason?: string;
  waiting_reason?: string;
  payload?: Record<string, unknown>;
};
export type Model = Entity & {
  name: string;
  role: "vision" | "chat" | "embedding";
  protocol: "chat" | "responses";
  base_url: string;
  api_key?: string;
  has_api_key?: boolean;
  model: string;
  thinking: "omit" | "enabled" | "disabled";
  reasoning_effort?: string | null;
  temperature?: number | null;
  top_p?: number | null;
  max_output_tokens: number;
  max_tokens_field: "max_completion_tokens" | "max_tokens";
  context_tokens: number;
  timeout_seconds: number;
  retries: number;
  max_tool_rounds: number;
  max_concurrency: number;
  credential_max_concurrency?: number | null;
  effective_max_concurrency?: number;
  store: boolean;
  strict_tools: boolean;
  include_encrypted_reasoning?: boolean;
  image_tokens?: number;
  extra_body: Record<string, unknown>;
  organization?: string;
  project?: string;
  auth_scope?: string;
  windows: { start: string; end: string }[];
  timezone: string;
  embedding_dimensions?: number | null;
  embedding_revision?: string;
  document_prefix?: string;
  query_prefix?: string;
};
export type Schedule = {
  not_before?: number;
  delay_seconds?: number;
  bypass_window?: boolean;
};
export type ExportSnapshot = {
  id: string;
  questions: Question[];
  mode: "practice" | "review";
  include_answer: boolean;
  include_explanation: boolean;
  include_knowledge: boolean;
  blank_lines: number;
  status?: string;
  url?: string;
};
export const questionTypes: Record<QuestionType, string> = {
  single_choice: "单选题",
  multiple_choice: "多选题",
  fill_blank: "填空题",
  short_answer: "简答题",
};
export const roleLabels: Record<Model["role"], string> = {
  vision: "视觉模型",
  chat: "讲解模型",
  embedding: "向量嵌入模型",
};
export const jobKindLabels: Record<string, string> = {
  model_test: "模型连接测试",
  question_extract: "整理题目",
  question_explain: "生成讲解",
  book_process: "整理教材",
  page_recognize: "识别教材页",
  book_index: "建立教材索引",
  suggestion_regenerate: "重新生成修改建议",
  export_pdf: "导出 PDF",
  search: "检索教材",
};
export const statusLabels: Record<string, string> = {
  draft: "草稿",
  pending: "等待处理",
  queued: "排队中",
  running: "处理中",
  completed: "已完成",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  deferred: "等待执行",
  ready: "就绪",
  processing: "处理中",
  indexing: "建立索引",
  needs_review: "待校对",
  waiting_review: "等待人工处理",
  waiting_window: "等待模型可用时段",
  waiting: "等待执行",
  paused: "已暂停",
};
export function assetUrl(id: string, preview = true): string {
  return `/api/assets/${encodeURIComponent(id)}/${preview ? "preview" : "file"}`;
}
export function answerText(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(answerText).join("、");
  return JSON.stringify(value);
}
