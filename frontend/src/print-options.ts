import type { ExportSnapshot } from "./types";

/** 练习版在渲染边界再次关闭答案，独立于服务端和导出表单校验。 */
export function printContentOptions(snapshot: ExportSnapshot) {
  const review = snapshot.mode === "review";
  return {
    answer: review && snapshot.include_answer,
    explanation: review && snapshot.include_explanation,
    knowledge: review && snapshot.include_knowledge,
    blankLines: review ? 0 : snapshot.blank_lines,
    practice: !review,
    printing: true,
  };
}
