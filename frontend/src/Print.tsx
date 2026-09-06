import { useEffect, useState } from "react";
import { api } from "./api";
import { QuestionContent } from "./Content";
import type { ExportSnapshot } from "./types";
import { printContentOptions } from "./print-options";
declare global {
  interface Window {
    __STUDYQUIP_PRINT_READY__?: boolean;
    __STUDYQUIP_PRINT_ERROR__?: string;
  }
}
export default function Print({ id }: { id: string }) {
  const [snapshot, setSnapshot] = useState<ExportSnapshot | null>(null),
    [error, setError] = useState("");
  useEffect(() => {
    window.__STUDYQUIP_PRINT_READY__ = false;
    window.__STUDYQUIP_PRINT_ERROR__ = undefined;
    const token = new URLSearchParams(location.search).get("token");
    if (!token) {
      setError("缺少导出访问凭证。");
      window.__STUDYQUIP_PRINT_ERROR__ = "缺少导出访问凭证。";
      return;
    }
    let active = true;
    api<ExportSnapshot>(
      `/export-snapshot/${encodeURIComponent(id)}?token=${encodeURIComponent(token)}`,
    )
      .then((data) => {
        if (active) setSnapshot(data);
      })
      .catch((e) => {
        if (active) {
          setError(e.message);
          window.__STUDYQUIP_PRINT_ERROR__ = e.message;
        }
      });
    return () => {
      active = false;
    };
  }, [id]);
  useEffect(() => {
    if (!snapshot) return;
    let active = true;
    async function ready() {
      try {
        await Promise.all([
          document.fonts.load('400 12px "StudyQuip Sans"', "教材错题"),
          document.fonts.load('700 12px "StudyQuip Sans"', "正确答案"),
        ]);
        await document.fonts.ready;
        if (
          !document.fonts.check('400 12px "StudyQuip Sans"', "教材") ||
          !document.fonts.check('700 12px "StudyQuip Sans"', "答案")
        )
          throw new Error("中文字体未能加载。");
        await Promise.all(
          Array.from(document.images).map(
            (img) =>
              new Promise<void>((resolve, reject) => {
                if (img.complete) {
                  img.naturalWidth
                    ? resolve()
                    : reject(new Error("题目配图未能加载。"));
                  return;
                }
                img.addEventListener("load", () => resolve(), { once: true });
                img.addEventListener(
                  "error",
                  () => reject(new Error("题目配图未能加载。")),
                  { once: true },
                );
              }),
          ),
        );
        if (document.querySelector(".katex-error"))
          throw new Error("部分公式无法渲染，请检查题目或解析中的公式。");
        await new Promise<void>((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
        );
        if (active) window.__STUDYQUIP_PRINT_READY__ = true;
      } catch (e) {
        if (active) {
          const message = (e as Error).message;
          setError(message);
          window.__STUDYQUIP_PRINT_ERROR__ = message;
        }
      }
    }
    void ready();
    return () => {
      active = false;
    };
  }, [snapshot]);
  if (error)
    return (
      <main className="print-error" role="alert">
        {error}
      </main>
    );
  if (!snapshot) return <main>正在准备导出内容…</main>;
  return (
    <main className="print-document">
      <header className="print-heading">
        <strong>StudyQuip</strong>
        <span>{snapshot.mode === "practice" ? "错题练习" : "错题复习"}</span>
      </header>
      {snapshot.questions.map((question, index) => (
        <QuestionContent
          key={question.id}
          question={question}
          index={index}
          {...printContentOptions(snapshot)}
        />
      ))}
    </main>
  );
}
