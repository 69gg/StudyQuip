import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { api, post, setCsrf } from "./api";
import type { Book, Subject } from "./types";
import { Button, Field, NoticeContext, useRemote } from "./ui";
const Questions = lazy(() => import("./Questions"));
const Books = lazy(() => import("./Books"));
const Models = lazy(() => import("./Models"));
const Tasks = lazy(() => import("./Tasks"));
const Search = lazy(() => import("./Search"));
const Print = lazy(() => import("./Print"));
type Session = {
  authenticated: boolean;
  csrf_token?: string;
  initialized: boolean;
};
type Theme = "light" | "dark" | "system";
const tabs = [
  ["questions", "错题"],
  ["books", "教材"],
  ["search", "检索"],
  ["tasks", "任务"],
  ["settings", "设置"],
];
export default function App() {
  const print = location.pathname.match(/^\/print\/([^/]+)$/);
  return (
    <Suspense fallback={<div className="loading">正在载入…</div>}>
      {print ? <Print id={decodeURIComponent(print[1])} /> : <Workspace />}
    </Suspense>
  );
}
function Workspace() {
  const [session, setSession] = useState<Session | null>(null),
    [error, setError] = useState(""),
    [tab, setTab] = useState(location.hash.slice(1) || "questions"),
    [theme, setTheme] = useState<Theme>(() => {
      const saved = localStorage.getItem("studyquip-theme");
      return saved === "light" || saved === "dark" ? saved : "system";
    }),
    [notice, setNotice] = useState<{ text: string; error: boolean } | null>(
      null,
    );
  const notify = useCallback(
    (text: string, error = false) => setNotice({ text, error }),
    [],
  );
  async function refreshSession() {
    try {
      const value = await api<Session>("/session");
      setCsrf(value.csrf_token || "");
      setSession(value);
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }
  useEffect(() => {
    void refreshSession();
    function unauthorized() {
      setSession((old) => (old ? { ...old, authenticated: false } : null));
    }
    window.addEventListener("studyquip:unauthorized", unauthorized);
    return () =>
      window.removeEventListener("studyquip:unauthorized", unauthorized);
  }, []);
  useEffect(() => {
    function hash() {
      setTab(location.hash.slice(1) || "questions");
    }
    window.addEventListener("hashchange", hash);
    return () => window.removeEventListener("hashchange", hash);
  }, []);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    function apply() {
      document.documentElement.dataset.theme =
        theme === "system" ? (mq.matches ? "dark" : "light") : theme;
      document.documentElement.style.colorScheme =
        document.documentElement.dataset.theme;
      localStorage.setItem("studyquip-theme", theme);
    }
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, [theme]);
  useEffect(() => {
    if (!notice) return;
    const id = window.setTimeout(() => setNotice(null), 6500);
    return () => clearTimeout(id);
  }, [notice]);
  return (
    <NoticeContext.Provider value={notify}>
      {!session?.authenticated ? (
        <Login session={session} error={error} onLogin={refreshSession} />
      ) : (
        <div className="app-shell">
          <aside className="sidebar">
            <a className="brand" href="#questions">
              <span className="brand-symbol" aria-hidden>
                ∴
              </span>
              StudyQuip
            </a>
            <nav aria-label="主导航">
              {tabs.map(([id, label]) => (
                <a
                  key={id}
                  href={`#${id}`}
                  className={tab === id ? "current" : ""}
                  aria-current={tab === id ? "page" : undefined}
                >
                  {label}
                </a>
              ))}
            </nav>
            <div className="sidebar-bottom">
              <label className="theme-select">
                <span>外观</span>
                <select
                  aria-label="外观主题"
                  value={theme}
                  onChange={(e) => setTheme(e.target.value as Theme)}
                >
                  <option value="system">跟随系统</option>
                  <option value="light">浅色</option>
                  <option value="dark">深色</option>
                </select>
              </label>
              <Button
                onClick={async () => {
                  try {
                    await post("/logout");
                    setCsrf("");
                    await refreshSession();
                  } catch (e) {
                    notify((e as Error).message, true);
                  }
                }}
              >
                退出登录
              </Button>
            </div>
          </aside>
          <main className="workspace">
            <Suspense fallback={<div className="loading">正在载入…</div>}>
              <WorkspaceContent tab={tab} />
            </Suspense>
          </main>
        </div>
      )}
      {notice && (
        <div
          className={`toast ${notice.error ? "error" : ""}`}
          role={notice.error ? "alert" : "status"}
        >
          <span>{notice.text}</span>
          <button aria-label="关闭提示" onClick={() => setNotice(null)}>
            ×
          </button>
        </div>
      )}
    </NoticeContext.Provider>
  );
}
function WorkspaceContent({ tab }: { tab: string }) {
  const subjects = useRemote<Subject[]>("/subjects", []),
    books = useRemote<Book[]>("/books", []);
  if (subjects.error || books.error)
    return (
      <div className="inline-error">
        {subjects.error || books.error}
        <Button
          onClick={() => {
            subjects.reload();
            books.reload();
          }}
        >
          重试
        </Button>
      </div>
    );
  if (tab === "books")
    return <Books subjects={subjects.data} onChanged={books.reload} />;
  if (tab === "settings")
    return (
      <Models subjects={subjects.data} onSubjectsChanged={subjects.reload} />
    );
  if (tab === "tasks") return <Tasks />;
  if (tab === "search")
    return <Search subjects={subjects.data} books={books.data} />;
  return <Questions subjects={subjects.data} books={books.data} />;
}
function Login({
  session,
  error,
  onLogin,
}: {
  session: Session | null;
  error: string;
  onLogin: () => Promise<void>;
}) {
  const [password, setPassword] = useState(""),
    [busy, setBusy] = useState(false),
    [failure, setFailure] = useState("");
  return (
    <main className="login-shell">
      <div className="login-brand">
        <span className="brand-symbol">∴</span>StudyQuip
      </div>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setFailure("");
          try {
            const value = await post<Session>("/login", { password });
            if (value?.csrf_token) setCsrf(value.csrf_token);
            setPassword("");
            await onLogin();
          } catch (err) {
            setFailure((err as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        <p className="eyebrow">个人学习工作台</p>
        <h1>继续你的学习。</h1>
        <p className="login-copy">错题、教材与理解，都留在这里。</p>
        {session?.initialized === false ? (
          <div className="gentle-notice">
            请先在服务器终端运行初始化命令并设置密码，然后刷新此页。
          </div>
        ) : (
          <>
            <Field label="访问密码">
              <input
                autoFocus
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="输入你的密码"
              />
            </Field>
            <Button
              kind="primary"
              type="submit"
              busy={busy}
              disabled={!session}
            >
              进入工作台 <span aria-hidden>→</span>
            </Button>
          </>
        )}
        {(failure || error) && (
          <div className="inline-error" role="alert">
            {failure || error}
          </div>
        )}
        {!session && (
          <Button onClick={() => void onLogin()}>重新连接服务器</Button>
        )}
      </form>
      <footer>Null · MIT License</footer>
    </main>
  );
}
