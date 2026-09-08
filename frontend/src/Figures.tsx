import { useMemo } from "react";
import DOMPurify from "dompurify";
import { parse } from "mathjs";
import type { PlotSpec, RenderedFigure } from "./types";

const PLOT = { width: 560, height: 340, margin: 38, samples: 480, ticks: 5 };
const FUNCTIONS = new Set([
  "sin",
  "cos",
  "tan",
  "asin",
  "acos",
  "atan",
  "sqrt",
  "abs",
  "log",
  "log10",
  "exp",
  "floor",
  "ceil",
  "min",
  "max",
]);
const SYMBOLS = new Set(["x", "pi", "e", ...FUNCTIONS]);

export function compileExpression(expression: string): (x: number) => number {
  const ast = parse(expression);
  ast.traverse((node) => {
    if (
      ![
        "ConstantNode",
        "OperatorNode",
        "SymbolNode",
        "FunctionNode",
        "ParenthesisNode",
      ].includes(node.type)
    )
      throw new Error("函数只支持 x、常数和常见数学运算。");
    if (
      node.type === "SymbolNode" &&
      !SYMBOLS.has((node as unknown as { name: string }).name)
    )
      throw new Error("函数包含不支持的变量或操作。");
    if (
      node.type === "FunctionNode" &&
      !FUNCTIONS.has((node as unknown as { name: string }).name)
    )
      throw new Error("函数不在支持的数学函数范围内。");
    if (
      node.type === "OperatorNode" &&
      !["+", "-", "*", "/", "^", "%"].includes(
        (node as unknown as { op: string }).op,
      )
    )
      throw new Error("不支持此运算符。");
  });
  const compiled = ast.compile();
  return (x) => {
    const value: unknown = compiled.evaluate({ x });
    return typeof value === "number" ? value : NaN;
  };
}

function Plot({ plot, title }: { plot: PlotSpec; title: string }) {
  const { width, height, margin, samples, ticks } = PLOT;
  const { x_min: xmin, x_max: xmax, y_min: ymin, y_max: ymax } = plot;
  if (
    ![xmin, xmax, ymin, ymax].every(Number.isFinite) ||
    xmin >= xmax ||
    ymin >= ymax
  )
    throw new Error("坐标范围无效。");
  const x = (value: number) =>
    margin + ((value - xmin) / (xmax - xmin)) * (width - margin * 2);
  const y = (value: number) =>
    height - margin - ((value - ymin) / (ymax - ymin)) * (height - margin * 2);
  const paths = plot.series.map((series) => {
    const evaluate = series.expression.trim()
      ? compileExpression(series.expression)
      : null;
    const points: [number, number][] = evaluate
      ? Array.from({ length: samples + 1 }, (_, i) => {
          const at = xmin + ((xmax - xmin) * i) / samples;
          return [at, evaluate(at)];
        })
      : series.points;
    let pen = false,
      previous: number | null = null;
    return points
      .map(([px, py]) => {
        if (
          !Number.isFinite(px) ||
          !Number.isFinite(py) ||
          px < xmin ||
          px > xmax ||
          py < ymin ||
          py > ymax
        ) {
          pen = false;
          previous = null;
          return "";
        }
        const joined =
          pen &&
          previous !== null &&
          Math.abs(py - previous) < (ymax - ymin) / 2;
        pen = true;
        previous = py;
        return `${joined ? "L" : "M"}${x(px).toFixed(2)},${y(py).toFixed(2)}`;
      })
      .join(" ");
  });
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={title || "函数坐标图"}
      className="coordinate-plot"
    >
      <rect width={width} height={height} fill="white" />
      {Array.from({ length: ticks + 1 }, (_, i) => {
        const px = xmin + ((xmax - xmin) * i) / ticks,
          py = ymin + ((ymax - ymin) * i) / ticks;
        return (
          <g key={i} fill="#555" fontSize="11">
            <path
              d={`M${x(px)},${margin}V${height - margin} M${margin},${y(py)}H${width - margin}`}
              stroke="#dedede"
            />
            <text x={x(px)} y={height - margin + 18} textAnchor="middle">
              {Number(px.toPrecision(3))}
            </text>
            <text x={margin - 8} y={y(py) + 4} textAnchor="end">
              {Number(py.toPrecision(3))}
            </text>
          </g>
        );
      })}
      <path
        d={`M${margin},${y(Math.max(ymin, Math.min(ymax, 0)))}H${width - margin} M${x(Math.max(xmin, Math.min(xmax, 0)))},${margin}V${height - margin}`}
        stroke="#444"
      />
      {paths.map((path, i) => (
        <path
          key={i}
          d={path}
          fill="none"
          stroke="#1b504a"
          strokeWidth="2"
          strokeDasharray={i % 2 ? "6 4" : undefined}
        />
      ))}
      <text
        x={width - margin}
        y={height - 6}
        textAnchor="end"
        fontSize="12"
        fill="#333"
      >
        {plot.x_label}
      </text>
      <text x={margin} y={18} fontSize="12" fill="#333">
        {plot.y_label}
      </text>
    </svg>
  );
}

export function RenderFigure({ figure }: { figure: RenderedFigure }) {
  const result = useMemo(() => {
    try {
      if (figure.kind === "plot") {
        if (!figure.plot) throw new Error("缺少坐标图配置。");
        return { content: Plot({ plot: figure.plot, title: figure.title }) };
      }
      const svg = DOMPurify.sanitize(figure.svg || "", {
        USE_PROFILES: { svg: true },
        FORBID_TAGS: [
          "foreignObject",
          "style",
          "image",
          "use",
          "a",
          "animate",
          "animateTransform",
          "set",
        ],
        FORBID_ATTR: ["href", "xlink:href", "style"],
      });
      if (!svg.includes("<svg")) throw new Error("SVG 插图无效。");
      return {
        content: (
          <img
            className="question-figure"
            src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`}
            alt={figure.title || "题目示意图"}
          />
        ),
      };
    } catch (error) {
      return { error: (error as Error).message };
    }
  }, [figure]);
  return (
    <figure
      className="rendered-figure"
      data-figure-error={result.error || undefined}
    >
      {result.error ? (
        <p role="alert">插图无法渲染：{result.error}</p>
      ) : (
        result.content
      )}
      {figure.title && <figcaption>{figure.title}</figcaption>}
      {figure.kind === "plot" &&
        figure.plot?.series.some((series) => series.label) && (
          <figcaption>
            {figure.plot.series
              .map((series) => series.label)
              .filter(Boolean)
              .join("；")}
          </figcaption>
        )}
    </figure>
  );
}
