import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api } from "./api";
import { statusLabels, type Schedule } from "./types";
export const NoticeContext = createContext<
  (message: string, error?: boolean) => void
>(() => {});
export function useNotice() {
  return useContext(NoticeContext);
}
export function useRemote<T>(path: string | null, initial: T) {
  const [data, setData] = useState<T>(initial);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [version, setVersion] = useState(0);
  useEffect(() => {
    if (!path) {
      setLoading(false);
      return;
    }
    let active = true;
    setLoading(true);
    setError("");
    api<T>(path)
      .then((v) => {
        if (active) setData(v);
      })
      .catch((e) => {
        if (active) setError(String(e.message));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [path, version]);
  return {
    data,
    setData,
    loading,
    error,
    reload: useCallback(() => setVersion((v) => v + 1), []),
  };
}
export function Button({
  children,
  busy = false,
  kind = "",
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  busy?: boolean;
  kind?: string;
}) {
  return (
    <button
      {...props}
      type={props.type || "button"}
      disabled={props.disabled || busy}
      className={`button ${kind} ${props.className || ""}`}
    >
      {busy ? <span className="spinner" /> : null}
      {children}
    </button>
  );
}
export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}
export function Check({
  label,
  checked,
  onChange,
  disabled = false,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label className="check">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span>{label}</span>
    </label>
  );
}
export function State({
  loading,
  error,
  empty,
  children,
}: {
  loading?: boolean;
  error?: string;
  empty?: boolean;
  children?: ReactNode;
}) {
  if (loading)
    return (
      <div className="empty" role="status">
        <span className="spinner" />
        正在加载…
      </div>
    );
  if (error)
    return (
      <div className="inline-error" role="alert">
        {error}
      </div>
    );
  if (empty) return <div className="empty">{children || "暂无内容"}</div>;
  return null;
}
export function Badge({ status }: { status?: string }) {
  return status ? (
    <span className={`badge ${status}`}>{statusLabels[status] || status}</span>
  ) : null;
}
export function Modal({
  title,
  onClose,
  children,
  wide = false,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    dialog?.showModal();
    return () => dialog?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      className={`modal ${wide ? "wide" : ""}`}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <div className="modal-title">
        <h2>{title}</h2>
        <Button aria-label="关闭窗口" onClick={onClose}>
          关闭
        </Button>
      </div>
      <div className="modal-body">{children}</div>
    </dialog>
  );
}
export function ScheduleDialog({
  title,
  description,
  onClose,
  onSubmit,
}: {
  title: string;
  description?: string;
  onClose: () => void;
  onSubmit: (schedule: Schedule) => Promise<unknown>;
}) {
  const [mode, setMode] = useState("now");
  const [delay, setDelay] = useState("10");
  const [when, setWhen] = useState("");
  const [bypass, setBypass] = useState(false);
  const [busy, setBusy] = useState(false);
  const notice = useNotice();
  async function submit() {
    setBusy(true);
    try {
      const schedule: Schedule = { bypass_window: bypass };
      if (mode === "delay")
        schedule.delay_seconds = Math.max(0, Number(delay)) * 60;
      if (mode === "at") {
        const parsed = new Date(when).getTime();
        if (!Number.isFinite(parsed) || parsed <= Date.now())
          throw new Error("请选择未来的执行时间。");
        schedule.not_before = parsed / 1000;
      }
      const result = (await onSubmit(schedule)) as
        { reused?: boolean } | undefined;
      notice(
        result?.reused
          ? "已有相同任务，已复用现有进度。"
          : "任务已提交，可在任务页查看进度。",
      );
      onClose();
    } catch (e) {
      notice((e as Error).message, true);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title={title} onClose={onClose}>
      <div className="stack">
        {description && <p className="hint">{description}</p>}
        <Field label="提交时间">
          <select value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="now">现在加入队列</option>
            <option value="delay">延迟一段时间</option>
            <option value="at">指定时间</option>
          </select>
        </Field>
        {mode === "delay" && (
          <Field label="延迟分钟">
            <input
              type="number"
              min="0"
              value={delay}
              onChange={(e) => setDelay(e.target.value)}
            />
          </Field>
        )}
        {mode === "at" && (
          <Field label="执行时间（当前设备时区）">
            <input
              type="datetime-local"
              value={when}
              onChange={(e) => setWhen(e.target.value)}
            />
          </Field>
        )}
        <Check
          checked={bypass}
          onChange={setBypass}
          label="此次忽略模型时段限制"
        />
        <p className="hint">
          任务会按模型可用时段和并发数量执行，关闭页面不影响进度。
        </p>
        <div className="actions">
          <Button onClick={onClose}>取消</Button>
          <Button kind="primary" busy={busy} onClick={submit}>
            加入队列
          </Button>
        </div>
      </div>
    </Modal>
  );
}
export function moveItem<T>(items: T[], index: number, offset: number): T[] {
  const next = [...items];
  const target = index + offset;
  if (target < 0 || target >= items.length) return next;
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}
export function formatTime(value: unknown): string {
  if (!value) return "—";
  const date = new Date(
    typeof value === "number" ? value * 1000 : String(value),
  );
  return Number.isNaN(date.getTime())
    ? String(value)
    : date.toLocaleString("zh-CN", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
}
