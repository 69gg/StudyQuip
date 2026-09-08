import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { compileExpression, RenderFigure } from "./Figures";

describe("安全函数图", () => {
  it("支持常见表达式并拒绝赋值、访问属性和执行代码", () => {
    expect(compileExpression("sqrt(x^2) + sin(pi/2)")(-3)).toBeCloseTo(4);
    for (const expression of [
      "a=2",
      "import('x')",
      "x.constructor",
      "evaluate('2+2')",
      "[1,2]",
    ]) {
      expect(() => compileExpression(expression)).toThrow();
    }
    const html = renderToStaticMarkup(
      <RenderFigure
        figure={{
          id: "p",
          kind: "plot",
          title: "坐标图",
          plot: {
            x_min: -2,
            x_max: 2,
            y_min: -1,
            y_max: 5,
            x_label: "x",
            y_label: "y",
            series: [{ label: "y=x²", expression: "x^2", points: [] }],
          },
        }}
      />,
    );
    expect(html).toContain("<svg");
    expect(html).not.toContain("data-figure-error");
  });
});
