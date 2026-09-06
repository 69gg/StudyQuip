import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import {
  BookOpen,
  House,
  ListTodo,
  LogOut,
  NotebookPen,
  Search as SearchIcon,
  Settings2,
} from "lucide-react";
import { api, post, setCsrf } from "./api";
import BrandMark from "./BrandMark";
import ThemeSwitch, { type Theme } from "./ThemeSwitch";
import type { Book, Subject } from "./types";
import { Button, Field, NoticeContext, State, useRemote } from "./ui";
const Home = lazy(() => import("./Home"));
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
const tabs = [
  { id: "home", label: "首页", icon: House },
  { id: "questions", label: "错题", icon: NotebookPen },
  { id: "books", label: "教材", icon: BookOpen },
  { id: "search", label: "检索", icon: SearchIcon },
  { id: "tasks", label: "任务", icon: ListTodo },
  { id: "settings", label: "设置", icon: Settings2 },
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
    [tab, setTab] = useState(location.hash.slice(1) || "home"),
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
      setTab(location.hash.slice(1) || "home");
    }
    window.addEventListener("hashchange", hash);
    return () => window.removeEventListener("hashchange", hash);
  }, []);
  useEffect(() => {
    const label =
      tabs.find((item) => item.id === tab.split("?")[0])?.label || "首页";
    document.title = `${label} · StudyQuip`;
  }, [tab]);
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
            <a className="brand" href="#home" aria-label="StudyQuip 首页">
              <BrandMark className="brand-symbol" />
              StudyQuip
            </a>
            <nav aria-label="主导航">
              {tabs.map(({ id, label, icon: Icon }) => (
                <a
                  key={id}
                  href={`#${id}`}
                  className={tab.split("?")[0] === id ? "current" : ""}
                  aria-current={tab.split("?")[0] === id ? "page" : undefined}
                >
                  <Icon size={18} strokeWidth={1.7} aria-hidden="true" />
                  <span>{label}</span>
                </a>
              ))}
            </nav>
            <div className="sidebar-bottom">
              <ThemeSwitch value={theme} onChange={setTheme} />
              <Button
                className="logout-button"
                aria-label="退出登录"
                title="退出登录"
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
                <LogOut size={16} strokeWidth={1.7} aria-hidden="true" />
                <span>退出登录</span>
              </Button>
            </div>
          </aside>
          <main className="workspace">
            <Suspense fallback={<div className="loading">正在载入…</div>}>
              <WorkspaceContent key={tab} tab={tab} />
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
  const [ready, setReady] = useState(false);
  const [page, query = ""] = tab.split("?", 2);
  const params = new URLSearchParams(query);
  const refreshLibrary = useCallback(() => {
    subjects.reload();
    books.reload();
  }, [subjects.reload, books.reload]);
  useEffect(() => {
    if (!subjects.loading && !books.loading && !subjects.error && !books.error)
      setReady(true);
  }, [subjects.loading, books.loading, subjects.error, books.error]);
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
  if (!ready) return <State loading />;
  if (page === "books")
    return (
      <Books
        subjects={subjects.data}
        onChanged={books.reload}
        startNew={params.get("new") === "1"}
        initialBookId={params.get("book")}
      />
    );
  if (page === "settings")
    return (
      <Models subjects={subjects.data} onSubjectsChanged={subjects.reload} />
    );
  if (page === "tasks") return <Tasks />;
  if (page === "search")
    return <Search subjects={subjects.data} books={books.data} />;
  if (page === "questions")
    return (
      <Questions
        subjects={subjects.data}
        books={books.data}
        startNew={params.get("new") === "1"}
        initialQuestionId={params.get("question")}
      />
    );
  return (
    <Home
      subjects={subjects.data}
      books={books.data}
      onRefresh={refreshLibrary}
    />
  );
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
        <BrandMark className="brand-symbol" size={42} />
        StudyQuip
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
