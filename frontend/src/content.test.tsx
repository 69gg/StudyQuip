import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { MathText, QuestionContent, formatAnswer } from "./Content";
import { printContentOptions } from "./print-options";
import { emptyQuestion } from "./Questions";
import type { ExportSnapshot, Question } from "./types";

const question: Question = {
  id: "question-test",
  revision: 1,
  subject_id: "subject-test",
  type: "single_choice",
  stem: "选出正确结果。",
  options: [
    { id: "option-a", text: "1" },
    { id: "option-b", text: "2" },
  ],
  answer: "option-b",
  answer_confirmed: true,
  asset_ids: ["marked-original"],
  reference_asset_ids: ["reference-answer"],
  figure_asset_ids: ["figure"],
  figures: [
    { id: "figure", url: "/api/assets/figure/preview?export_id=e&token=grant" },
  ],
  book_ids: [],
  notes: "不能混入导出正文的录入备注",
  reference_text: "不能混入导出正文的参考答案",
  explanation: { summary: "解题思路测试", knowledge_points: ["知识点测试"] },
};

describe("共用题目与打印渲染", () => {
  it("新建题目默认不请求优化做错原因，已有优化结果不会覆盖原文", () => {
    const draft = emptyQuestion("subject-test");
    expect(draft.error_reason).toBe("");
    expect(draft.optimize_error_reason).toBe(false);
    const original = "看错了题目的条件，还以为要求周长。";
    const explained: Question = {
      ...question,
      error_reason: original,
      optimize_error_reason: true,
      explanation: {
        error_reason_optimized: "审题时误读条件，混淆了求解目标。",
      },
    };
    const html = renderToStaticMarkup(
      <QuestionContent question={explained} explanation />,
    );
    expect(html).toContain("做错原因（AI 优化表述）");
    expect(html).toContain("审题时误读条件，混淆了求解目标。");
    expect(explained.error_reason).toBe(original);
  });

  it("未勾选、旧记录、空原文和过期讲解均不显示优化结果", () => {
    const cases: Partial<Question>[] = [
      {},
      { error_reason: "条件看错", optimize_error_reason: false },
      { error_reason: " \n\t", optimize_error_reason: true },
      {
        error_reason: "修改后的原因",
        optimize_error_reason: true,
        explanation_stale: true,
      },
    ];
    for (const fields of cases) {
      const html = renderToStaticMarkup(
        <QuestionContent
          question={{
            ...question,
            ...fields,
            explanation: {
              summary: "保留原有讲解",
              error_reason_optimized: "不应显示的旧优化结果",
            },
          }}
          explanation
        />,
      );
      expect(html).toContain("保留原有讲解");
      expect(html).not.toContain("不应显示的旧优化结果");
    }
  });

  it("练习版即使收到全部开启的标志，也不会出现答案、解析或知识点", () => {
    const snapshot: ExportSnapshot = {
      id: "e",
      questions: [question],
      mode: "practice",
      include_answer: true,
      include_explanation: true,
      include_knowledge: true,
      blank_lines: 3,
    };
    const html = renderToStaticMarkup(
      <QuestionContent
        question={question}
        {...printContentOptions(snapshot)}
      />,
    );
    expect(html).not.toContain("正确答案");
    expect(html).not.toContain("解题思路测试");
    expect(html).not.toContain("知识点测试");
    expect(html).not.toContain("answer-lines");
    expect(html).not.toContain("marked-original");
    expect(html).not.toContain("reference-answer");
    expect(html).not.toContain("录入备注");
    expect(html).toContain("token=grant");
  });

  it("复习版的答案、解析和知识点可以分别选择", () => {
    const html = renderToStaticMarkup(
      <QuestionContent question={question} knowledge />,
    );
    expect(html).toContain("知识点测试");
    expect(html).not.toContain("解题思路测试");
    expect(html).not.toContain("正确答案");
    expect(formatAnswer(question)).toBe("B");
    expect(
      formatAnswer({
        ...question,
        type: "multiple_choice",
        answer: ["option-b", "option-a"],
      }),
    ).toBe("B、A");
  });

  it("支持行内公式、独立公式与化学式，并阻止原始HTML和远程图片", () => {
    const html = renderToStaticMarkup(
      <MathText
        text={
          '行内 \\(x^2\\)\n\n\\[\\frac{1}{2}\\]\n\n$\\ce{H2O}$\n\n<img src="bad" onerror="alert(1)" />\n\n![外链图片](https://example.invalid/image.png)'
        }
      />,
    );
    expect(html).toContain('class="katex"');
    expect(html).toContain('class="katex-display"');
    expect(html).not.toContain("katex-error");
    expect(html).not.toContain("<img");
    expect(html).not.toContain("onerror");
  });
});

describe("嵌套大题与听力打印", () => {
  const nested: Question = {
    ...question,
    type: "composite",
    stem: "根据材料回答",
    options: [],
    figure_asset_ids: [],
    figures: [],
    materials: [
      {
        id: "listening",
        kind: "listening",
        title: "听力一",
        text: "只在复习版出现的听力文稿",
        audio_asset_id: "audio",
      },
    ],
    parts: [
      {
        ...question,
        id: "group",
        type: "composite",
        stem: "第二层材料",
        parts: [
          {
            ...question,
            id: "choice",
            figures: [],
            figure_asset_ids: [],
            error_reason: "误听时间",
          },
          {
            ...question,
            id: "short",
            type: "short_answer",
            stem: "简述理由",
            options: [],
            figures: [],
            figure_asset_ids: [],
          },
        ],
      },
    ],
  };
  it("深层编号、只为简答留白、练习不泄露听力文稿及答案", () => {
    const html = renderToStaticMarkup(
      <QuestionContent
        question={nested}
        index={0}
        practice
        printing
        blankLines={3}
      />,
    );
    expect(html).toContain("1.1.1.");
    expect(html).toContain("1.1.2.");
    expect((html.match(/class="answer-lines"/g) || []).length).toBe(1);
    expect(html).not.toContain("听力文稿");
    expect(html).not.toContain("<audio");
    expect(html).not.toContain("正确答案");
  });
  it("复习打印包含文稿、做错原因和叶节点答案", () => {
    const html = renderToStaticMarkup(
      <QuestionContent
        question={nested}
        index={0}
        answer
        explanation
        knowledge
        printing
      />,
    );
    expect(html).toContain("只在复习版出现的听力文稿");
    expect(html).toContain("误听时间");
    expect(html).toContain("做错原因（原文）");
    expect((html.match(/<h4>正确答案<\/h4>/g) || []).length).toBe(2);
    expect(html).not.toContain("<audio");
  });
});
