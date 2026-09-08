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
  | "single_choice"
  | "multiple_choice"
  | "fill_blank"
  | "short_answer"
  | "composite";
export type PlotSpec = {
  x_min: number;
  x_max: number;
  y_min: number;
  y_max: number;
  x_label: string;
  y_label: string;
  series: { label: string; expression: string; points: [number, number][] }[];
};
export type RenderedFigure = {
  id: string;
  kind: "svg" | "plot";
  title: string;
  svg?: string | null;
  plot?: PlotSpec | null;
};
export type Material = {
  id: string;
  kind: "text" | "listening";
  title: string;
  text: string;
  audio_asset_id?: string | null;
  audio_generated_from?: string | null;
};
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
  parts?: Question[];
  materials?: Material[];
  audio_pending_roles?: Model["role"][];
  rendered_figures?: RenderedFigure[];
  figure_requirements?: string[];
  status?: string;
  explanation?: Record<string, unknown>;
  explanation_stale?: boolean;
  formatting_warnings?: string[];
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
  blocking?: boolean;
  recovering?: boolean;
  resume?: {
    available: boolean;
    has_saved_progress: boolean;
    reason?: string | null;
  };
  reused?: boolean;
  resource_title?: string;
  book_id?: string;
  question_ids?: string[];
  attempts?: number;
  input?: Record<string, unknown>;
  progress?: {
    phase?: string;
    total_pages?: number;
    recognized_pages?: number;
    processed_pages?: number;
    pending_pages?: number;
    review_pages?: number;
    skipped_pages?: number;
    blocks?: number;
    nodes?: number;
    summarized_nodes?: number;
    current_page?: number;
    embedding_total?: number;
    embedding_completed?: number;
    completed_units?: number;
    completed_stages?: number;
    last_activity_at?: number;
    usage?: {
      requests?: number;
      input_tokens?: number;
      output_tokens?: number;
    };
    active_requests?: RequestActivity[];
    embedding_activity?: RequestActivity;
  };
};
export type RequestActivity = {
  state: string;
  at?: number;
  next_at?: number;
  model?: string;
  revision?: number;
  page?: number;
  rounds?: number;
  attempt?: number;
  format_attempt?: number;
  format_limit?: number;
  reason?: string;
  streaming?: boolean;
  received_events?: number;
  first_received_at?: number;
  last_received_at?: number;
  output_characters?: number;
  reasoning_characters?: number;
  tool_argument_characters?: number;
  tool_names?: string[];
};
export type Model = Entity & {
  name: string;
  role:
    | "book_vision"
    | "book_text"
    | "question_vision"
    | "question_text"
    | "embedding"
    | "speech_recognition"
    | "speech_synthesis";
  protocol: "chat" | "responses";
  base_url: string;
  api_key?: string;
  has_api_key?: boolean;
  model: string;
  thinking: "omit" | "enabled" | "disabled";
  stream?: boolean;
  stream_include_usage?: boolean;
  voice?: string;
  audio_language?: string | null;
  audio_speed?: number | null;
  tool_choice?: "required" | "auto" | "omit";
  reasoning_effort?: string | null;
  temperature?: number | null;
  top_p?: number | null;
  max_output_tokens: number | null;
  max_tokens_field: "max_completion_tokens" | "max_tokens";
  context_tokens: number | null;
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
  composite: "大题",
};
export const roleLabels: Record<Model["role"], string> = {
  book_vision: "教材图片模型",
  book_text: "教材文本模型",
  question_vision: "题目图片模型",
  question_text: "题目文本模型",
  embedding: "向量嵌入模型",
  speech_recognition: "语音识别模型",
  speech_synthesis: "语音合成模型",
};
export const roleDescriptions: Record<Model["role"], string> = {
  book_vision: "识别教材图片与 PDF 页面，提取文字和插图描述。",
  book_text: "顺序整理教材、衔接跨页内容、生成目录概述与修改建议。",
  question_vision: "识别题目图片和参考解析图片，提取题目字段。",
  question_text: "整理纯文本题目，生成讲解、知识点与已勾选的错因优化。",
  embedding: "生成教材与查询文本的向量，供语义检索使用。",
  speech_recognition: "将听力录音转为文稿，使用兼容的音频识别接口。",
  speech_synthesis: "将听力文稿合成为录音，可配置服务商提供的音色。",
};
export const jobKindLabels: Record<string, string> = {
  model_test: "模型连接测试",
  question_extract: "整理题目",
  question_explain: "生成讲解",
  question_audio: "补全听力资料",
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

export function questionNodes(question: Question): Question[] {
  const result: Question[] = [],
    stack = [question];
  while (stack.length) {
    const node = stack.pop()!;
    result.push(node);
    stack.push(...[...(node.parts || [])].reverse());
  }
  return result;
}
export function questionTitle(question: Question): string {
  return (
    question.stem ||
    question.materials?.find((material) => material.title)?.title ||
    questionNodes(question).find((node) => node.stem)?.stem ||
    (question.type === "composite" ? "大题" : "待整理的题目")
  );
}
