import ReactMarkdown from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/contrib/mhchem";
import { answerText, assetUrl, type Question } from "./types";
export function MathText({
  text,
  className = "",
}: {
  text: unknown;
  className?: string;
}) {
  const source = (typeof text === "string" ? text : answerText(text))
    .replace(
      /\\\[([\s\S]*?)\\\]/g,
      (_, value: string) => `\n$$\n${value}\n$$\n`,
    )
    .replace(/\\\(([\s\S]*?)\\\)/g, (_, value: string) => "$" + value + "$");
  return (
    <div className={`rich-text ${className}`}>
      <ReactMarkdown
        skipHtml
        remarkPlugins={[remarkMath]}
        rehypePlugins={[
          [rehypeKatex, { throwOnError: false, trust: false, strict: "warn" }],
        ]}
        components={{
          img: ({ alt }) => <span>{alt ? `[${alt}]` : ""}</span>,
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noreferrer">
              {children}
            </a>
          ),
        }}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
}
export function Explanation({
  value,
  knowledge = true,
  optimizedErrorReason = false,
}: {
  value?: Record<string, unknown>;
  knowledge?: boolean;
  optimizedErrorReason?: boolean;
}) {
  if (!value) return null;
  const entries = Object.entries(value).filter(
    ([key, v]) =>
      v != null &&
      v !== "" &&
      (!Array.isArray(v) || v.length > 0) &&
      !(
        key === "has_textbook_evidence" &&
        v === true &&
        Array.isArray(value.citations) &&
        value.citations.length > 0
      ) &&
      !["raw", "reasoning", "thinking", "answer", "correct_answer"].includes(
        key,
      ) &&
      (key !== "error_reason_optimized" || optimizedErrorReason) &&
      (knowledge ||
        !["knowledge_points", "knowledge", "concepts"].includes(key)),
  );
  const labels: Record<string, string> = {
    summary: "解题思路",
    analysis: "解析",
    explanation: "解析",
    steps: "解题步骤",
    solution: "解题过程",
    knowledge_points: "知识点",
    knowledge: "知识点",
    concepts: "知识点",
    citations: "教材依据",
    sources: "教材依据",
    references: "参考依据",
    error_analysis: "错误分析",
    wrong_answer_analysis: "错误分析",
    error_reason_optimized: "做错原因（AI 优化表述）",
    warnings: "需要核对",
    notes: "注意事项",
    confidence: "可信度",
    conflict: "答案核对",
    answer_conflict: "答案核对",
    has_textbook_evidence: "教材依据",
    textbook_evidence: "教材依据",
    no_evidence: "依据提示",
  };
  return (
    <div className="explanation">
      {entries.map(([key, val]) => (
        <section key={key} data-explanation-section={key}>
          <h4>{labels[key] || "补充说明"}</h4>
          <Value value={val} />
        </section>
      ))}
    </div>
  );
}
function Value({ value }: { value: unknown }) {
  if (typeof value === "boolean")
    return <span>{value ? "已找到相关教材" : "未找到教材依据"}</span>;
  if (Array.isArray(value))
    return (
      <div className="value-list">
        {value.map((item, i) => (
          <div key={i}>
            <Value value={item} />
          </div>
        ))}
      </div>
    );
  if (value && typeof value === "object") {
    const obj = value as Record<string, unknown>;
    const keys = [
      "title",
      "name",
      "book_title",
      "node_path",
      "chapter_path",
      "path",
      "description",
      "content",
      "text",
      "quote",
      "reason",
      "detail",
      "explanation",
    ];
    const selected = keys.filter((k) => obj[k]);
    if (selected.length)
      return (
        <div>
          {selected.map((k) => (
            <MathText
              key={k}
              text={
                Array.isArray(obj[k])
                  ? (obj[k] as unknown[]).join(" › ")
                  : obj[k]
              }
            />
          ))}
        </div>
      );
    return (
      <div>
        {Object.entries(obj)
          .filter(
            ([k]) =>
              !k.endsWith("_id") &&
              !["page", "page_number", "page_index"].includes(k),
          )
          .map(([k, v]) => (
            <Value key={k} value={v} />
          ))}
      </div>
    );
  }
  return <MathText text={value} />;
}
export function QuestionContent({
  question,
  index,
  answer = false,
  explanation = false,
  knowledge = false,
  blankLines = 0,
}: {
  question: Question;
  index?: number;
  answer?: boolean;
  explanation?: boolean;
  knowledge?: boolean;
  blankLines?: number;
}) {
  return (
    <article className="question-content">
      <div className="question-prompt">
        {index !== undefined && (
          <div className="question-number">{index + 1}.</div>
        )}
        <MathText text={question.stem} />
        {(
          question.figures ||
          question.figure_asset_ids?.map((id) => ({
            id,
            url: assetUrl(id, false),
            name: "题目配图",
          }))
        )?.map((figure) => (
          <img
            key={figure.id}
            className="question-figure"
            src={figure.url}
            alt={figure.name || "题目配图"}
          />
        ))}
        {question.options?.length > 0 && (
          <div className="options-preview">
            {question.options.map((option, i) => (
              <div key={option.id}>
                <span>{String.fromCharCode(65 + i)}.</span>
                <MathText text={option.text} />
              </div>
            ))}
          </div>
        )}
        {blankLines > 0 && (
          <div className="answer-lines">
            {Array.from({ length: blankLines }, (_, i) => (
              <div key={i} />
            ))}
          </div>
        )}
      </div>
      {answer && (
        <section className="answer-section">
          <h4>正确答案</h4>
          <MathText text={formatAnswer(question)} />
        </section>
      )}
      {explanation && (
        <Explanation
          value={question.explanation}
          knowledge={knowledge}
          optimizedErrorReason={
            question.optimize_error_reason === true &&
            !!question.error_reason?.trim() &&
            !question.explanation_stale
          }
        />
      )}{" "}
      {!explanation && knowledge && question.explanation && (
        <Explanation
          value={Object.fromEntries(
            Object.entries(question.explanation).filter(([k]) =>
              ["knowledge", "knowledge_points", "concepts"].includes(k),
            ),
          )}
        />
      )}
    </article>
  );
}
export function formatAnswer(question: Question): string {
  if (
    question.type === "single_choice" ||
    question.type === "multiple_choice"
  ) {
    const values = Array.isArray(question.answer)
      ? question.answer
      : [question.answer];
    return values
      .map((value) => {
        const i = question.options?.findIndex((o) => o.id === value);
        return i >= 0 ? String.fromCharCode(65 + i) : answerText(value);
      })
      .join("、");
  }
  return answerText(question.answer);
}
