import { useState } from "react";
import { post, put, remove } from "./api";
import { roleLabels, type Model, type Subject } from "./types";
import {
  Button,
  Check,
  Field,
  Modal,
  ScheduleDialog,
  State,
  useNotice,
  useRemote,
} from "./ui";
export default function Models({
  subjects,
  onSubjectsChanged,
}: {
  subjects: Subject[];
  onSubjectsChanged: () => void;
}) {
  const remote = useRemote<Model[]>("/models", []);
  const [editing, setEditing] = useState<Model | null>(null),
    [testing, setTesting] = useState<Model | null>(null),
    [subject, setSubject] = useState("");
  const notice = useNotice();
  function create(): Model {
    return {
      id: "",
      revision: 0,
      name: "",
      role: "chat",
      protocol: "chat",
      base_url: "",
      model: "",
      thinking: "omit",
      tool_choice: "required",
      reasoning_effort: null,
      max_output_tokens: 4096,
      max_tokens_field: "max_completion_tokens",
      context_tokens: 24000,
      timeout_seconds: 120,
      retries: 2,
      max_tool_rounds: 8,
      max_concurrency: 4,
      credential_max_concurrency: null,
      store: false,
      strict_tools: false,
      image_tokens: 2048,
      include_encrypted_reasoning: true,
      extra_body: {},
      windows: [],
      timezone:
        Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai",
    };
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>设置</h1>
          <p>管理模型连接、处理时段与科目。</p>
        </div>
        <Button kind="primary" onClick={() => setEditing(create())}>
          ＋ 添加模型配置
        </Button>
      </div>
      <h2>模型</h2>
      <State
        loading={remote.loading}
        error={remote.error}
        empty={!remote.data.length}
      >
        先配置视觉、讲解和向量嵌入模型。视觉与讲解可以使用同一个模型。
      </State>
      <div className="record-list">
        {remote.data.map((model) => (
          <div className="model-row" key={model.id}>
            <button
              className="record-main"
              onClick={() => setEditing({ ...model, api_key: "" })}
            >
              <div className="record-meta">
                <span>{roleLabels[model.role]}</span>
                <span>
                  {model.role === "embedding"
                    ? "Embeddings"
                    : model.protocol === "chat"
                      ? "Chat Completions"
                      : "Responses"}
                </span>
              </div>
              <h3>{model.name || model.model}</h3>
              <p className="model-subtitle">
                {model.model} · 并发{" "}
                {model.effective_max_concurrency || model.max_concurrency}
                {model.windows?.length
                  ? ` · ${model.windows.map((w) => `${w.start}–${w.end}`).join("、")}`
                  : " · 全天可用"}
              </p>
            </button>
            <div className="inline-actions">
              <Button onClick={() => setTesting(model)}>测试连接</Button>
              <Button onClick={() => setEditing({ ...model, api_key: "" })}>
                编辑
              </Button>
            </div>
          </div>
        ))}
      </div>
      <section className="settings-section">
        <h2>科目</h2>
        <div className="subject-list">
          {subjects.map((s) => (
            <span key={s.id}>{s.name}</span>
          ))}
        </div>
        <form
          className="inline-actions"
          onSubmit={async (e) => {
            e.preventDefault();
            try {
              await post("/subjects", { name: subject.trim() });
              setSubject("");
              onSubjectsChanged();
              notice("科目已添加。");
            } catch (error) {
              notice((error as Error).message, true);
            }
          }}
        >
          <input
            aria-label="新科目名称"
            required
            placeholder="添加科目"
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
          />
          <Button type="submit">添加</Button>
        </form>
      </section>
      <section className="settings-section about">
        <h2>StudyQuip</h2>
        <p>
          作者 Null · <a href="mailto:pylindex@qq.com">pylindex@qq.com</a> · MIT
          License
        </p>
      </section>
      {editing && (
        <ModelEditor
          initial={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            remote.reload();
          }}
        />
      )}
      {testing && (
        <ScheduleDialog
          title={`测试 ${testing.name || testing.model}`}
          onClose={() => setTesting(null)}
          onSubmit={(timing) => post(`/models/${testing.id}/test`, timing)}
        />
      )}
    </>
  );
}
function ModelEditor({
  initial,
  onClose,
  onSaved,
}: {
  initial: Model;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [model, setModel] = useState(initial),
    [extra, setExtra] = useState(
      JSON.stringify(initial.extra_body || {}, null, 2),
    ),
    [busy, setBusy] = useState(false);
  const notice = useNotice();
  function update<K extends keyof Model>(key: K, value: Model[K]) {
    setModel((old) => ({ ...old, [key]: value }));
  }
  function numberField(
    key: keyof Model,
    label: string,
    hint?: string,
    nullable = false,
    min = 0,
    step = "1",
  ) {
    return (
      <Field label={label} hint={hint}>
        <input
          type="number"
          min={min}
          step={step}
          value={(model[key] ?? "") as string | number}
          onChange={(e) =>
            update(
              key,
              (e.target.value === "" && nullable
                ? null
                : Number(e.target.value)) as never,
            )
          }
          required={!nullable}
        />
      </Field>
    );
  }
  return (
    <Modal
      title={model.id ? "编辑模型配置" : "添加模型配置"}
      onClose={onClose}
      wide
    >
      <form
        className="stack"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          try {
            const parsed = JSON.parse(extra);
            if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
              throw new Error("扩展参数必须是 JSON 对象。");
            const payload: Partial<Model> = {
              ...model,
              reasoning_effort: model.reasoning_effort?.trim() || null,
              extra_body: parsed,
            };
            if (model.id && !payload.api_key) delete payload.api_key;
            if (!model.id) payload.api_key = model.api_key || "";
            if (model.id) await put(`/models/${model.id}`, payload);
            else await post("/models", payload);
            notice("模型配置已保存。");
            onSaved();
          } catch (error) {
            notice((error as Error).message, true);
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="form-grid">
          <Field label="配置名称">
            <input
              required
              value={model.name}
              onChange={(e) => update("name", e.target.value)}
              placeholder="便于识别的名称"
            />
          </Field>
          <Field label="用途">
            <select
              value={model.role}
              onChange={(e) => update("role", e.target.value as Model["role"])}
            >
              {Object.entries(roleLabels).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <div className="form-grid">
          <Field label="服务地址">
            <input
              required
              type="url"
              value={model.base_url}
              onChange={(e) => update("base_url", e.target.value)}
              placeholder="https://服务地址/v1"
              autoComplete="off"
            />
          </Field>
          <Field label="模型名称">
            <input
              required
              value={model.model}
              onChange={(e) => update("model", e.target.value)}
              placeholder="填写服务商提供的模型 ID"
            />
          </Field>
        </div>
        <div className="form-grid">
          <Field
            label="API Key"
            hint={
              model.has_api_key
                ? "已保存密钥；留空将保留现有密钥。"
                : "服务商要求时填写；密钥仅由服务器保存。"
            }
          >
            <input
              type="password"
              autoComplete="new-password"
              value={model.api_key || ""}
              onChange={(e) => update("api_key", e.target.value)}
            />
          </Field>
          {model.role !== "embedding" && (
            <Field label="API 类型">
              <select
                value={model.protocol}
                onChange={(e) =>
                  update("protocol", e.target.value as Model["protocol"])
                }
              >
                <option value="chat">Chat Completions</option>
                <option value="responses">Responses</option>
              </select>
            </Field>
          )}
        </div>
        {model.role !== "embedding" && (
          <>
            <h3 className="form-section-title">生成参数</h3>
            <div className="form-grid">
              <Field
                label="Thinking"
                hint={
                  model.protocol === "chat"
                    ? "启用时发送 thinking.type=enabled，关闭时发送 disabled。"
                    : "按 Responses 协议及模型支持情况处理。"
                }
              >
                <select
                  value={model.thinking}
                  onChange={(e) =>
                    update("thinking", e.target.value as Model["thinking"])
                  }
                >
                  <option value="omit">不发送，使用服务商默认值</option>
                  <option value="enabled">开启</option>
                  <option value="disabled">关闭</option>
                </select>
              </Field>
              <Field
                label="工具选择策略"
                hint="DeepSeek 思考模式请选择不发送；应用仍会校验工具返回结构。"
              >
                <select
                  value={model.tool_choice ?? "required"}
                  onChange={(e) =>
                    update(
                      "tool_choice",
                      e.target.value as Model["tool_choice"],
                    )
                  }
                >
                  <option value="required">强制调用工具（required）</option>
                  <option value="auto">由模型选择（auto）</option>
                  <option value="omit">不发送（服务商默认）</option>
                </select>
              </Field>
              <Field
                label="思考强度（reasoning effort）"
                hint="按服务商支持填写；留空不发送。"
              >
                <input
                  list="reasoning-levels"
                  value={model.reasoning_effort || ""}
                  onChange={(e) => update("reasoning_effort", e.target.value)}
                  placeholder="例如 low、high、max"
                />
                <datalist id="reasoning-levels">
                  {[
                    "none",
                    "minimal",
                    "low",
                    "medium",
                    "high",
                    "xhigh",
                    "max",
                  ].map((v) => (
                    <option value={v} key={v} />
                  ))}
                </datalist>
              </Field>
              {numberField(
                "temperature",
                "Temperature",
                "留空时不发送。",
                true,
                0,
                "0.1",
              )}
              {numberField("top_p", "Top P", "留空时不发送。", true, 0, "0.05")}
              {numberField(
                "context_tokens",
                "上下文预算（tokens）",
                "用于请求前的内容预算。",
                false,
                1,
              )}
              {numberField(
                "max_output_tokens",
                "最大输出（tokens）",
                undefined,
                false,
                1,
              )}
            </div>
            {model.protocol === "chat" && (
              <Field label="输出长度字段">
                <select
                  value={model.max_tokens_field}
                  onChange={(e) =>
                    update(
                      "max_tokens_field",
                      e.target.value as Model["max_tokens_field"],
                    )
                  }
                >
                  <option value="max_completion_tokens">
                    max_completion_tokens
                  </option>
                  <option value="max_tokens">max_tokens</option>
                </select>
              </Field>
            )}
          </>
        )}
        {model.role === "embedding" && (
          <>
            <h3 className="form-section-title">向量设置</h3>
            <div className="form-grid">
              {numberField(
                "embedding_dimensions",
                "向量维度",
                "留空使用模型默认维度。",
                true,
                1,
              )}
              <Field label="模型修订标识" hint="同名模型发生变化时更新此标识。">
                <input
                  value={model.embedding_revision || ""}
                  onChange={(e) => update("embedding_revision", e.target.value)}
                />
              </Field>
              <Field label="文档前缀">
                <input
                  value={model.document_prefix || ""}
                  onChange={(e) => update("document_prefix", e.target.value)}
                />
              </Field>
              <Field label="查询前缀">
                <input
                  value={model.query_prefix || ""}
                  onChange={(e) => update("query_prefix", e.target.value)}
                />
              </Field>
            </div>
            <p className="hint">修改向量模型或维度后，教材需要重建索引。</p>
          </>
        )}
        <h3 className="form-section-title">执行时段与并发</h3>
        <div className="form-grid">
          {numberField(
            "max_concurrency",
            "每个模型最大并发",
            "同服务、同凭据、同名模型的配置共享此限制。",
            false,
            1,
          )}
          {numberField(
            "credential_max_concurrency",
            "凭据总并发上限（可选）",
            "留空不设置跨模型的总上限。",
            true,
            1,
          )}
          <Field label="时区">
            <input
              required
              value={model.timezone}
              onChange={(e) => update("timezone", e.target.value)}
              placeholder="Asia/Shanghai"
            />
          </Field>
        </div>
        {model.windows?.map((window, i) => (
          <div className="window-row" key={i}>
            <Field label="开始">
              <input
                type="time"
                required
                value={window.start}
                onChange={(e) =>
                  update(
                    "windows",
                    model.windows.map((w, j) =>
                      j === i ? { ...w, start: e.target.value } : w,
                    ),
                  )
                }
              />
            </Field>
            <span>至</span>
            <Field label="结束">
              <input
                type="time"
                required
                value={window.end}
                onChange={(e) =>
                  update(
                    "windows",
                    model.windows.map((w, j) =>
                      j === i ? { ...w, end: e.target.value } : w,
                    ),
                  )
                }
              />
            </Field>
            <Button
              onClick={() =>
                update(
                  "windows",
                  model.windows.filter((_, j) => i !== j),
                )
              }
            >
              移除
            </Button>
          </div>
        ))}
        <div className="inline-actions">
          <Button
            onClick={() =>
              update("windows", [
                ...(model.windows || []),
                { start: "22:00", end: "08:00" },
              ])
            }
          >
            ＋ 添加可用时段
          </Button>
          <span className="hint">
            不设置则全天可用；结束早于开始表示跨午夜。
          </span>
        </div>
        <details>
          <summary>高级参数</summary>
          <div className="stack">
            <div className="form-grid">
              {numberField(
                "timeout_seconds",
                "请求超时（秒）",
                undefined,
                false,
                1,
              )}
              {numberField("retries", "网络重试次数")}
              {model.role !== "embedding" &&
                numberField(
                  "max_tool_rounds",
                  "最多工具调用轮数",
                  undefined,
                  false,
                  1,
                )}
              {model.role !== "embedding" &&
                numberField(
                  "image_tokens",
                  "每张图片预估 token",
                  "用于上下文预算估算。",
                  false,
                  1,
                )}
              <Field label="Organization">
                <input
                  value={model.organization || ""}
                  onChange={(e) => update("organization", e.target.value)}
                />
              </Field>
              <Field label="Project">
                <input
                  value={model.project || ""}
                  onChange={(e) => update("project", e.target.value)}
                />
              </Field>
              <Field label="认证范围标识">
                <input
                  value={model.auth_scope || ""}
                  onChange={(e) => update("auth_scope", e.target.value)}
                />
              </Field>
            </div>
            {model.role !== "embedding" && (
              <>
                <Check
                  label="启用严格工具格式（需要服务商支持）"
                  checked={model.strict_tools}
                  onChange={(v) => update("strict_tools", v)}
                />
                <Check
                  label="允许服务商保存响应（store）"
                  checked={model.store}
                  onChange={(v) => update("store", v)}
                />
                {model.protocol === "responses" && (
                  <Check
                    label="在工具调用间保留加密思考内容"
                    checked={model.include_encrypted_reasoning ?? true}
                    onChange={(v) => update("include_encrypted_reasoning", v)}
                  />
                )}
              </>
            )}
            <Field
              label="额外请求参数（JSON）"
              hint="按服务商文档填写；不能覆盖应用的模型、消息、工具等保留字段。"
            >
              <textarea
                className="code-input"
                rows={7}
                value={extra}
                onChange={(e) => setExtra(e.target.value)}
                spellCheck={false}
              />
            </Field>
          </div>
        </details>
        <div className="row-between">
          {model.id ? (
            <Button
              kind="danger"
              onClick={async () => {
                if (!window.confirm("确定删除此模型配置吗？")) return;
                try {
                  await remove(`/models/${model.id}`);
                  onSaved();
                } catch (e) {
                  notice((e as Error).message, true);
                }
              }}
            >
              删除配置
            </Button>
          ) : (
            <span />
          )}
          <div className="inline-actions">
            <Button onClick={onClose}>取消</Button>
            <Button kind="primary" type="submit" busy={busy}>
              保存配置
            </Button>
          </div>
        </div>
      </form>
    </Modal>
  );
}
