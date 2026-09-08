import { useRef, useState } from "react";
import { v4 as uuid } from "uuid";
import { api } from "./api";
import { RenderFigure } from "./Figures";
import {
  assetUrl,
  questionTypes,
  type Asset,
  type Material,
  type Question,
  type RenderedFigure,
} from "./types";
import { Button, Field, Modal, moveItem, useNotice } from "./ui";

export function atPath(root: Question, path: number[]): Question {
  return path.reduce((node, index) => node.parts?.[index] || node, root);
}
export function changeAt(
  root: Question,
  path: number[],
  change: (node: Question) => Question,
): Question {
  if (!path.length) return change(root);
  const [index, ...rest] = path;
  return {
    ...root,
    parts: root.parts?.map((part, i) =>
      i === index ? changeAt(part, rest, change) : part,
    ),
  };
}
export function QuestionNavigator({
  root,
  path,
  onSelect,
  onChange,
  create,
}: {
  root: Question;
  path: number[];
  onSelect: (path: number[]) => void;
  onChange: (value: Question) => void;
  create: () => Question;
}) {
  const current = atPath(root, path);
  const parent = atPath(root, path.slice(0, -1));
  const rows: { node: Question; path: number[]; label: string }[] = [];
  const stack = [{ node: root, path: [] as number[], label: "整题" }];
  while (stack.length) {
    const row = stack.pop()!;
    rows.push(row);
    stack.push(
      ...(row.node.parts || [])
        .map((node, i) => ({
          node,
          path: [...row.path, i],
          label: [...row.path, i].map((n) => n + 1).join("."),
        }))
        .reverse(),
    );
  }
  return (
    <div className="question-navigation">
      <Field label="当前编辑位置">
        <select
          value={path.join(".")}
          onChange={(event) =>
            onSelect(
              event.target.value
                ? event.target.value.split(".").map(Number)
                : [],
            )
          }
        >
          {rows.map((row) => (
            <option key={row.path.join(".")} value={row.path.join(".")}>
              {row.label} · {questionTypes[row.node.type]}
              {row.node.stem ? ` · ${row.node.stem.slice(0, 32)}` : ""}
            </option>
          ))}
        </select>
      </Field>
      <div className="inline-actions">
        {path.length > 0 && (
          <Button onClick={() => onSelect(path.slice(0, -1))}>上一级</Button>
        )}
        {current.type === "composite" && (
          <Button
            onClick={() => {
              const index = current.parts?.length || 0;
              onChange(
                changeAt(root, path, (node) => ({
                  ...node,
                  parts: [...(node.parts || []), create()],
                })),
              );
              onSelect([...path, index]);
            }}
          >
            添加小题
          </Button>
        )}
        {path.length > 0 && (
          <>
            {([-1, 1] as const).map((direction) => (
              <Button
                key={direction}
                disabled={
                  path.at(-1)! + direction < 0 ||
                  path.at(-1)! + direction >= (parent.parts?.length || 0)
                }
                onClick={() => {
                  onChange(
                    changeAt(root, path.slice(0, -1), (node) => ({
                      ...node,
                      parts: moveItem(
                        node.parts || [],
                        path.at(-1)!,
                        direction,
                      ),
                    })),
                  );
                  onSelect([...path.slice(0, -1), path.at(-1)! + direction]);
                }}
              >
                {direction < 0 ? "上移" : "下移"}
              </Button>
            ))}
            <Button
              kind="danger"
              onClick={() => {
                if (window.confirm("删除当前小题及其全部子题？")) {
                  onChange(
                    changeAt(root, path.slice(0, -1), (node) => ({
                      ...node,
                      parts: node.parts?.filter((_, i) => i !== path.at(-1)),
                    })),
                  );
                  onSelect(path.slice(0, -1));
                }
              }}
            >
              删除当前小题
            </Button>
          </>
        )}
      </div>
      {!!current.parts?.length && (
        <div className="part-links">
          {current.parts.map((part, i) => (
            <button
              type="button"
              key={part.id}
              onClick={() => onSelect([...path, i])}
            >
              <span>{i + 1}</span>
              <span>{part.stem || questionTypes[part.type]}</span>
              <small>{questionTypes[part.type]}</small>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function MaterialsEditor({
  value,
  onChange,
}: {
  value: Material[];
  onChange: (value: Material[]) => void;
}) {
  return (
    <section className="stack compact materials-editor">
      <div className="row-between">
        <h3>
          材料 <small className="hint">可选</small>
        </h3>
        <div className="inline-actions">
          {(["text", "listening"] as const).map((kind) => (
            <Button
              key={kind}
              onClick={() =>
                onChange([...value, { id: uuid(), kind, title: "", text: "" }])
              }
            >
              {kind === "text" ? "添加文本材料" : "添加听力"}
            </Button>
          ))}
        </div>
      </div>
      {value.map((material, index) => (
        <MaterialEditor
          key={material.id}
          material={material}
          onChange={(next) =>
            onChange(value.map((old) => (old.id === material.id ? next : old)))
          }
          onDelete={() =>
            onChange(value.filter((old) => old.id !== material.id))
          }
          onMove={(direction) => onChange(moveItem(value, index, direction))}
          first={index === 0}
          last={index === value.length - 1}
        />
      ))}
    </section>
  );
}
function MaterialEditor({
  material,
  onChange,
  onDelete,
  onMove,
  first,
  last,
}: {
  material: Material;
  onChange: (material: Material) => void;
  onDelete: () => void;
  onMove: (direction: -1 | 1) => void;
  first: boolean;
  last: boolean;
}) {
  const input = useRef<HTMLInputElement>(null),
    notice = useNotice();
  const [busy, setBusy] = useState(false);
  const listening = material.kind === "listening";
  return (
    <div className="material-editor stack compact">
      <div className="row-between">
        <strong>{listening ? "听力材料" : "文本材料"}</strong>
        <div className="inline-actions">
          <Button disabled={first} onClick={() => onMove(-1)}>
            上移
          </Button>
          <Button disabled={last} onClick={() => onMove(1)}>
            下移
          </Button>
          <Button onClick={onDelete}>移除</Button>
        </div>
      </div>
      <Field label="材料标题（可选）">
        <input
          value={material.title}
          onChange={(event) =>
            onChange({ ...material, title: event.target.value })
          }
        />
      </Field>
      {listening && (
        <>
          <input
            ref={input}
            type="file"
            hidden
            accept="audio/*,.m4a,.mp3,.wav,.ogg,.flac,.webm"
            onChange={async (event) => {
              const file = event.target.files?.[0];
              if (!file) return;
              setBusy(true);
              try {
                const form = new FormData();
                form.append("file", file);
                const asset = await api<Asset>("/assets", {
                  method: "POST",
                  body: form,
                });
                if (!asset.mime.startsWith("audio/"))
                  throw new Error("请选择音频文件。");
                onChange({
                  ...material,
                  audio_asset_id: asset.id,
                  audio_generated_from: null,
                });
              } catch (error) {
                notice((error as Error).message, true);
              } finally {
                setBusy(false);
                if (input.current) input.current.value = "";
              }
            }}
          />
          <div className="inline-actions">
            <Button busy={busy} onClick={() => input.current?.click()}>
              {material.audio_asset_id ? "替换录音" : "上传录音（可选）"}
            </Button>
            {material.audio_asset_id && (
              <Button
                onClick={() =>
                  onChange({
                    ...material,
                    audio_asset_id: null,
                    audio_generated_from: null,
                  })
                }
              >
                移除录音
              </Button>
            )}
          </div>
          {material.audio_asset_id && (
            <audio
              controls
              preload="none"
              src={assetUrl(material.audio_asset_id, false)}
            />
          )}
          {material.audio_generated_from && (
            <small className="hint">AI 合成音频；修改文稿后可重新生成。</small>
          )}
        </>
      )}
      <Field label={listening ? "听力文稿（可选）" : "材料正文"}>
        <textarea
          rows={5}
          value={material.text}
          onChange={(event) =>
            onChange({ ...material, text: event.target.value })
          }
        />
      </Field>
      {listening && (
        <p className="hint">
          录音与文稿至少提供一项。保存后补全缺失项；未配置对应语音模型时保留原材料。练习
          PDF 隐藏听力文稿。
        </p>
      )}
    </div>
  );
}

export function FiguresEditor({
  value,
  onChange,
}: {
  value: RenderedFigure[];
  onChange: (value: RenderedFigure[]) => void;
}) {
  const [editing, setEditing] = useState<RenderedFigure | null>(null);
  return (
    <section className="stack compact">
      <div className="row-between">
        <h3>
          可编辑插图 <small className="hint">可选</small>
        </h3>
        <div className="inline-actions">
          <Button
            onClick={() =>
              setEditing({
                id: uuid(),
                kind: "svg",
                title: "",
                svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 240"><line x1="40" y1="200" x2="360" y2="40" stroke="black" /></svg>',
              })
            }
          >
            SVG 示意图
          </Button>
          <Button
            onClick={() =>
              setEditing({
                id: uuid(),
                kind: "plot",
                title: "",
                plot: {
                  x_min: -5,
                  x_max: 5,
                  y_min: -5,
                  y_max: 5,
                  x_label: "x",
                  y_label: "y",
                  series: [{ label: "", expression: "x", points: [] }],
                },
              })
            }
          >
            函数 / 坐标图
          </Button>
        </div>
      </div>
      {value.map((figure) => (
        <div key={figure.id}>
          <RenderFigure figure={figure} />
          <div className="inline-actions">
            <Button onClick={() => setEditing(figure)}>编辑插图</Button>
            <Button
              onClick={() =>
                onChange(value.filter((old) => old.id !== figure.id))
              }
            >
              移除
            </Button>
          </div>
        </div>
      ))}
      {editing && (
        <FigureEditor
          initial={editing}
          onClose={() => setEditing(null)}
          onApply={(next) => {
            onChange(
              value.some((old) => old.id === next.id)
                ? value.map((old) => (old.id === next.id ? next : old))
                : [...value, next],
            );
            setEditing(null);
          }}
        />
      )}
    </section>
  );
}
function FigureEditor({
  initial,
  onClose,
  onApply,
}: {
  initial: RenderedFigure;
  onClose: () => void;
  onApply: (next: RenderedFigure) => void;
}) {
  const [figure, setFigure] = useState(initial),
    [source, setSource] = useState(initial.svg || ""),
    [error, setError] = useState("");
  const preview = {
    ...figure,
    ...(figure.kind === "svg" ? { svg: source } : {}),
  };
  return (
    <Modal
      title={figure.kind === "svg" ? "编辑 SVG 示意图" : "编辑函数 / 坐标图"}
      onClose={onClose}
    >
      <div className="stack">
        <Field label="插图标题（可选）">
          <input
            value={figure.title}
            onChange={(event) =>
              setFigure({ ...figure, title: event.target.value })
            }
          />
        </Field>
        {figure.kind === "svg" ? (
          <Field label="静态 SVG">
            <textarea
              className="code-input"
              rows={9}
              value={source}
              onChange={(event) => setSource(event.target.value)}
            />
          </Field>
        ) : (
          figure.plot && (
            <>
              <div className="form-grid">
                {(["x_min", "x_max", "y_min", "y_max"] as const).map((key) => (
                  <Field
                    key={key}
                    label={
                      {
                        x_min: "x 最小值",
                        x_max: "x 最大值",
                        y_min: "y 最小值",
                        y_max: "y 最大值",
                      }[key]
                    }
                  >
                    <input
                      type="number"
                      step="any"
                      value={figure.plot![key]}
                      onChange={(event) =>
                        setFigure({
                          ...figure,
                          plot: {
                            ...figure.plot!,
                            [key]: Number(event.target.value),
                          },
                        })
                      }
                    />
                  </Field>
                ))}
              </div>
              {figure.plot.series.map((series, i) => (
                <div className="stack compact" key={i}>
                  <Field label={`曲线 ${i + 1} · 图例`}>
                    <input
                      value={series.label}
                      onChange={(event) =>
                        setFigure({
                          ...figure,
                          plot: {
                            ...figure.plot!,
                            series: figure.plot!.series.map((old, n) =>
                              n === i
                                ? { ...old, label: event.target.value }
                                : old,
                            ),
                          },
                        })
                      }
                    />
                  </Field>
                  <Field
                    label="函数 y ="
                    hint="例如 x^2、sin(x)、sqrt(x)。清空函数时使用已有坐标点。"
                  >
                    <input
                      value={series.expression}
                      onChange={(event) =>
                        setFigure({
                          ...figure,
                          plot: {
                            ...figure.plot!,
                            series: figure.plot!.series.map((old, n) =>
                              n === i
                                ? { ...old, expression: event.target.value }
                                : old,
                            ),
                          },
                        })
                      }
                    />
                  </Field>
                  <Field label="坐标点（JSON 数组，可选）">
                    <textarea
                      rows={2}
                      defaultValue={JSON.stringify(series.points)}
                      onBlur={(event) => {
                        try {
                          const points: unknown = JSON.parse(
                            event.target.value,
                          );
                          if (
                            !Array.isArray(points) ||
                            points.some(
                              (point) =>
                                !Array.isArray(point) ||
                                point.length !== 2 ||
                                !point.every(Number.isFinite),
                            )
                          )
                            throw new Error("坐标点需要 [[x,y], ...] 数组。");
                          setFigure({
                            ...figure,
                            plot: {
                              ...figure.plot!,
                              series: figure.plot!.series.map((old, n) =>
                                n === i
                                  ? {
                                      ...old,
                                      points: points as [number, number][],
                                    }
                                  : old,
                              ),
                            },
                          });
                          setError("");
                        } catch (e) {
                          setError((e as Error).message);
                        }
                      }}
                    />
                  </Field>
                  <Button
                    onClick={() =>
                      setFigure({
                        ...figure,
                        plot: {
                          ...figure.plot!,
                          series: figure.plot!.series.filter((_, n) => n !== i),
                        },
                      })
                    }
                  >
                    删除曲线
                  </Button>
                </div>
              ))}
              <Button
                onClick={() =>
                  setFigure({
                    ...figure,
                    plot: {
                      ...figure.plot!,
                      series: [
                        ...figure.plot!.series,
                        { expression: "x", label: "", points: [] },
                      ],
                    },
                  })
                }
              >
                添加曲线
              </Button>
            </>
          )
        )}
        <RenderFigure figure={preview} />
        {error && <p role="alert">{error}</p>}
        <Button
          kind="primary"
          disabled={!!error}
          onClick={() => onApply(preview)}
        >
          应用插图
        </Button>
      </div>
    </Modal>
  );
}
