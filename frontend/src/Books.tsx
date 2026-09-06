import { useRef, useState } from "react";
import { v4 as uuidv4 } from "uuid";
import { api, post, put, remove } from "./api";
import { assetUrl, type Book, type Entity, type Subject } from "./types";
import {
  Badge,
  Button,
  Field,
  Modal,
  ScheduleDialog,
  State,
  useNotice,
  useRemote,
} from "./ui";
import { MathText } from "./Content";
import Uploads from "./Uploads";
type Node = Entity & {
  parent_id: string | null;
  title: string;
  order: number;
  is_root?: boolean;
  summary?: string;
  summary_stale?: boolean;
};
type Block = Entity & {
  title?: string;
  node_id: string | null;
  text: string;
  type: string;
  order: number;
  human_protected?: boolean;
  archived?: boolean;
};
type Page = Entity & {
  text?: string;
  page_index?: number;
  source_asset_id?: string;
  image_asset?: { id: string };
  image_asset_id?: string;
  status?: string;
  error?: string;
  source_type?: string;
};
type Suggestion = Entity & {
  status: string;
  reason?: string;
  before?: Block[];
  after?: Block[];
  operations?: unknown[];
};
export function orderNodes(nodes: Node[]): { node: Node; depth: number }[] {
  const result: { node: Node; depth: number }[] = [];
  const visited = new Set<string>();
  function visit(parent: string | null, depth: number) {
    nodes
      .filter((n) => (n.parent_id || null) === parent)
      .sort((a, b) => a.order - b.order)
      .forEach((node) => {
        if (visited.has(node.id)) return;
        visited.add(node.id);
        result.push({ node, depth });
        visit(node.id, depth + 1);
      });
  }
  visit(null, 0);
  nodes
    .filter((n) => !visited.has(n.id))
    .forEach((node) => result.push({ node, depth: 0 }));
  return result;
}
export default function Books({
  subjects,
  onChanged,
}: {
  subjects: Subject[];
  onChanged: () => void;
}) {
  const remote = useRemote<Book[]>("/books", []);
  const [selected, setSelected] = useState<Book | null>(null),
    [editing, setEditing] = useState<Book | null>(null),
    [subject, setSubject] = useState(""),
    [search, setSearch] = useState("");
  function reload() {
    remote.reload();
    onChanged();
  }
  if (selected)
    return (
      <BookDetail
        book={selected}
        subjects={subjects}
        onBack={() => setSelected(null)}
        onChanged={reload}
      />
    );
  const list = remote.data.filter(
    (b) =>
      (!subject || b.subject_id === subject) &&
      b.title.toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>教材</h1>
          <p>保留原文结构，让知识有出处。</p>
        </div>
        <div className="inline-actions">
          <Button onClick={reload}>刷新</Button>
          <Button
            kind="primary"
            onClick={() =>
              setEditing({
                id: "",
                revision: 0,
                title: "",
                subject_id: subjects[0]?.id || "",
                text: "",
                asset_ids: [],
              })
            }
          >
            ＋ 导入教材
          </Button>
        </div>
      </div>
      <div className="filter-bar">
        <input
          aria-label="搜索教材"
          placeholder="搜索教材名称…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select
          aria-label="科目筛选"
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
        >
          <option value="">全部科目</option>
          {subjects.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
      </div>
      <State loading={remote.loading} error={remote.error} empty={!list.length}>
        导入纯文本、图片或整本 PDF，建立自己的教材库。
      </State>
      <div className="record-list">
        {list.map((book) => (
          <button
            key={book.id}
            className="book-row"
            onClick={() => setSelected(book)}
          >
            <div className="book-spine" aria-hidden>
              <span>
                {subjects
                  .find((s) => s.id === book.subject_id)
                  ?.name?.slice(0, 2) || "书"}
              </span>
            </div>
            <div className="record-main">
              <h3>{book.title}</h3>
              <div className="record-meta">
                <span>
                  {subjects.find((s) => s.id === book.subject_id)?.name}
                </span>
                <Badge status={book.status || "draft"} />
              </div>
            </div>
            <span className="row-arrow">↗</span>
          </button>
        ))}
      </div>
      {editing && (
        <BookForm
          initial={editing}
          subjects={subjects}
          onClose={() => setEditing(null)}
          onSaved={(book) => {
            reload();
            setEditing(null);
            setSelected(book);
          }}
        />
      )}
    </>
  );
}
function BookForm({
  initial,
  subjects,
  onClose,
  onSaved,
}: {
  initial: Book;
  subjects: Subject[];
  onClose: () => void;
  onSaved: (book: Book) => void;
}) {
  const [book, setBook] = useState(initial),
    [busy, setBusy] = useState(false);
  const notice = useNotice();
  return (
    <Modal title={book.id ? "教材设置" : "导入教材"} onClose={onClose} wide>
      <form
        className="stack"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          try {
            const saved = book.id
              ? await put<Book>(`/books/${book.id}`, book)
              : await post<Book>("/books", book);
            onSaved(saved);
          } catch (error) {
            notice((error as Error).message, true);
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="form-grid">
          <Field label="教材名称">
            <input
              required
              value={book.title}
              onChange={(e) => setBook({ ...book, title: e.target.value })}
              placeholder="例如：物理·必修第一册"
            />
          </Field>
          <Field label="科目">
            <select
              required
              value={book.subject_id}
              onChange={(e) => setBook({ ...book, subject_id: e.target.value })}
            >
              <option value="">选择科目</option>
              {subjects.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Uploads
          allowPdf
          label="PDF、图片或文本文件（按顺序处理）"
          ids={book.asset_ids || []}
          onChange={(asset_ids) => setBook({ ...book, asset_ids })}
        />
        <Field label="纯文本内容（可选）">
          <textarea
            rows={10}
            value={book.text || ""}
            onChange={(e) => setBook({ ...book, text: e.target.value })}
            placeholder="也可以直接粘贴教材全文。"
          />
        </Field>
        <Field
          label="教材概述额外请求上限（可选）"
          hint="留空不限。只限制目录概述生成；用完后可增加上限，再到任务页面继续处理。"
        >
          <input
            type="number"
            min="0"
            step="1"
            value={book.extra_processing_budget ?? ""}
            onChange={(e) =>
              setBook({
                ...book,
                extra_processing_budget:
                  e.target.value === "" ? null : Number(e.target.value),
              })
            }
          />
        </Field>
        <div className="actions">
          <Button onClick={onClose}>取消</Button>
          <Button kind="primary" type="submit" busy={busy}>
            保存教材
          </Button>
        </div>
      </form>
    </Modal>
  );
}
function BookDetail({
  book: initial,
  subjects,
  onBack,
  onChanged,
}: {
  book: Book;
  subjects: Subject[];
  onBack: () => void;
  onChanged: () => void;
}) {
  const [book, setBook] = useState(initial),
    [tab, setTab] = useState("article"),
    [nodeId, setNodeId] = useState(""),
    [editing, setEditing] = useState(false),
    [block, setBlock] = useState<Block | null>(null),
    [node, setNode] = useState<Node | null>(null),
    [schedule, setSchedule] = useState<string | null>(null);
  const nodes = useRemote<Node[]>(`/books/${book.id}/nodes`, []),
    blocks = useRemote<Block[]>(`/books/${book.id}/blocks`, []),
    pages = useRemote<Page[]>(`/books/${book.id}/pages`, []),
    suggestions = useRemote<Suggestion[]>(`/books/${book.id}/suggestions`, []);
  const notice = useNotice();
  const pendingOperation = useRef<{ signature: string; id: string } | null>(
    null,
  );
  function refresh() {
    nodes.reload();
    blocks.reload();
    pages.reload();
    suggestions.reload();
    void api<Book>(`/books/${book.id}`)
      .then(setBook)
      .catch((e) => notice(e.message, true));
    onChanged();
  }
  async function operations(ops: unknown[]) {
    const signature = JSON.stringify(ops);
    if (pendingOperation.current?.signature !== signature)
      pendingOperation.current = { signature, id: uuidv4() };
    const receipt = await post<{ status: string; reason?: string }>(
      `/books/${book.id}/operations`,
      {
        group_id: pendingOperation.current.id,
        operations: ops,
        reason: "用户在教材校对界面修改",
      },
    );
    pendingOperation.current = null;
    if (receipt.status === "rejected")
      throw new Error(receipt.reason || "内容已变化，请重新载入后修改。");
    refresh();
  }
  const sorted = orderNodes(nodes.data);
  const visible = blocks.data
    .filter((b) => !b.archived && (!nodeId || b.node_id === nodeId))
    .sort((a, b) => a.order - b.order);
  return (
    <>
      <Button kind="back" onClick={onBack}>
        ← 返回教材库
      </Button>
      <div className="page-heading">
        <div>
          <h1>{book.title}</h1>
          <div className="record-meta">
            <span>{subjects.find((s) => s.id === book.subject_id)?.name}</span>
            <Badge status={book.status} />
          </div>
        </div>
        <div className="inline-actions">
          <Button onClick={refresh}>刷新结果</Button>
          <Button onClick={() => setEditing(true)}>教材设置</Button>
          <Button
            kind="primary"
            onClick={() => setSchedule(`/books/${book.id}/process`)}
          >
            识别与整理
          </Button>
        </div>
      </div>
      <div className="tabs">
        <Button
          kind={tab === "article" ? "active" : ""}
          onClick={() => setTab("article")}
        >
          整理后的正文
        </Button>
        <Button
          kind={tab === "pages" ? "active" : ""}
          onClick={() => setTab("pages")}
        >
          原页校对
        </Button>
        <Button
          kind={tab === "suggestions" ? "active" : ""}
          onClick={() => setTab("suggestions")}
        >
          修改建议
          {suggestions.data.filter((s) => s.status === "pending").length
            ? ` · ${suggestions.data.filter((s) => s.status === "pending").length}`
            : ""}
        </Button>
      </div>
      {tab === "article" && (
        <div className="textbook-workspace">
          <aside className="toc">
            <div className="row-between">
              <h3>目录</h3>
              <Button
                onClick={() =>
                  setNode({
                    id: "",
                    revision: 0,
                    parent_id: nodes.data.find((n) => n.is_root)?.id || null,
                    title: "",
                    order: nodes.data.length,
                  })
                }
              >
                ＋
              </Button>
            </div>
            <button
              className={!nodeId ? "selected" : ""}
              onClick={() => setNodeId("")}
            >
              全部正文
            </button>
            {sorted.map(({ node, depth }) => (
              <div className="toc-row" key={node.id}>
                <button
                  className={node.id === nodeId ? "selected" : ""}
                  style={{
                    paddingInlineStart: `${Math.min(depth, 6) * 12 + 10}px`,
                  }}
                  onClick={() => setNodeId(node.id)}
                >
                  {node.title}
                </button>
                <button
                  aria-label={`编辑目录 ${node.title}`}
                  onClick={() => setNode(node)}
                >
                  ✎
                </button>
              </div>
            ))}
            <State loading={nodes.loading} error={nodes.error} />
          </aside>
          <div className="article-workspace">
            <div className="row-between">
              <h2>
                {nodes.data.find((n) => n.id === nodeId)?.title || "教材正文"}
              </h2>
              <Button
                onClick={() =>
                  setBlock({
                    id: "",
                    revision: 0,
                    node_id:
                      nodeId || nodes.data.find((n) => n.is_root)?.id || null,
                    text: "",
                    type: "paragraph",
                    order: visible.length,
                  })
                }
              >
                ＋ 添加段落
              </Button>
            </div>
            <State
              loading={blocks.loading}
              error={blocks.error}
              empty={!visible.length}
            >
              尚无整理后的正文。先运行“识别与整理”，或手动添加内容。
            </State>
            {visible.map((b) => (
              <section key={b.id} className={`textbook-block ${b.type}`}>
                <div className="block-heading">
                  <span>
                    {(
                      {
                        paragraph: "正文",
                        figure: "插图说明",
                        example: "例题",
                        aside: "补充内容",
                      } as Record<string, string>
                    )[b.type] || "正文"}
                    {b.human_protected ? " · 已人工校对" : ""}
                  </span>
                  <Button onClick={() => setBlock(b)}>编辑</Button>
                </div>
                <MathText text={b.text} />
              </section>
            ))}
          </div>
        </div>
      )}
      {tab === "pages" && (
        <div className="pages-list">
          <State
            loading={pages.loading}
            error={pages.error}
            empty={!pages.data.length}
          >
            原页将在教材处理后显示。
          </State>
          {pages.data.map((page, i) => (
            <PageEditor
              key={`${page.id}-${page.revision}`}
              page={page}
              index={i}
              bookId={book.id}
              onChanged={refresh}
              onSchedule={setSchedule}
            />
          ))}
        </div>
      )}
      {tab === "suggestions" && (
        <div>
          <State
            loading={suggestions.loading}
            error={suggestions.error}
            empty={!suggestions.data.length}
          >
            目前没有待处理的修改建议。
          </State>
          {suggestions.data.map((s) => (
            <section className="suggestion" key={s.id}>
              <div className="row-between">
                <h3>{s.reason || "内容修改建议"}</h3>
                <span className="badge">
                  {(
                    {
                      pending: "待确认",
                      accepted: "已采纳",
                      ignored: "已忽略",
                      stale: "内容已变化",
                    } as Record<string, string>
                  )[s.status] || s.status}
                </span>
              </div>
              <div className="diff-grid">
                <div>
                  <h4>修改前</h4>
                  {s.before?.length ? (
                    s.before.map((b, i) => (
                      <MathText text={b.text ?? b.title ?? ""} key={i} />
                    ))
                  ) : (
                    <p className="hint">新增内容</p>
                  )}
                </div>
                <div>
                  <h4>修改后</h4>
                  {s.after?.map((b, i) => (
                    <MathText text={b.text ?? b.title ?? ""} key={i} />
                  ))}
                </div>
              </div>
              <div className="actions">
                <Button
                  onClick={() =>
                    setSchedule(
                      `/books/${book.id}/suggestions/${s.id}/regenerate`,
                    )
                  }
                >
                  重新生成建议
                </Button>
                {s.status === "pending" && (
                  <>
                    <Button
                      onClick={async () => {
                        try {
                          await post(
                            `/books/${book.id}/suggestions/${s.id}/ignore`,
                          );
                          refresh();
                        } catch (e) {
                          notice((e as Error).message, true);
                        }
                      }}
                    >
                      忽略
                    </Button>
                    <Button
                      kind="primary"
                      onClick={async () => {
                        try {
                          const accepted = await post<{ status: string }>(
                            `/books/${book.id}/suggestions/${s.id}/accept`,
                          );
                          refresh();
                          notice(
                            accepted.status === "stale"
                              ? "原内容已变化，请重新生成修改建议。"
                              : "已采纳修改。",
                            accepted.status === "stale",
                          );
                        } catch (e) {
                          notice((e as Error).message, true);
                        }
                      }}
                    >
                      采纳修改
                    </Button>
                  </>
                )}
              </div>
            </section>
          ))}
        </div>
      )}
      <div className="section-end">
        <Button
          kind="danger"
          onClick={async () => {
            if (!window.confirm(`确定删除《${book.title}》及其整理内容吗？`))
              return;
            try {
              await remove(`/books/${book.id}`);
              onChanged();
              onBack();
            } catch (e) {
              notice((e as Error).message, true);
            }
          }}
        >
          删除教材
        </Button>
      </div>
      {editing && (
        <BookForm
          initial={book}
          subjects={subjects}
          onClose={() => setEditing(false)}
          onSaved={(value) => {
            setBook(value);
            setEditing(false);
            refresh();
          }}
        />
      )}
      {node && (
        <NodeEditor
          node={node}
          nodes={nodes.data}
          onClose={() => setNode(null)}
          onSave={async (value) => {
            await operations([
              {
                op: "node",
                node: {
                  ...(value.id
                    ? { id: value.id, base_revision: value.revision }
                    : {}),
                  parent_id: value.parent_id,
                  title: value.title,
                  order: value.order,
                },
              },
            ]);
            setNode(null);
          }}
        />
      )}
      {block && (
        <BlockEditor
          block={block}
          nodes={nodes.data}
          bookId={book.id}
          onClose={() => setBlock(null)}
          onSave={async (value) => {
            await operations([
              value.id
                ? {
                    op: "update",
                    id: value.id,
                    base_revision: value.revision,
                    changes: {
                      text: value.text,
                      node_id: value.node_id,
                      type: value.type,
                      order: value.order,
                    },
                  }
                : {
                    op: "insert",
                    block: {
                      text: value.text,
                      node_id: value.node_id,
                      type: value.type,
                      order: value.order,
                    },
                  },
            ]);
            setBlock(null);
          }}
          onArchive={async () => {
            await operations([
              { op: "archive", id: block.id, base_revision: block.revision },
            ]);
            setBlock(null);
          }}
          onRestore={async (revision) => {
            await operations([
              {
                op: "restore",
                id: block.id,
                base_revision: block.revision,
                restore_revision: revision,
              },
            ]);
            setBlock(null);
          }}
        />
      )}
      {schedule && (
        <ScheduleDialog
          title="教材处理任务"
          onClose={() => setSchedule(null)}
          onSubmit={(timing) => post(schedule, timing)}
        />
      )}
    </>
  );
}
function NodeEditor({
  node: initial,
  nodes,
  onClose,
  onSave,
}: {
  node: Node;
  nodes: Node[];
  onClose: () => void;
  onSave: (node: Node) => Promise<void>;
}) {
  const [node, setNode] = useState(initial),
    [busy, setBusy] = useState(false);
  const notice = useNotice();
  return (
    <Modal title={node.id ? "校正目录" : "添加目录项"} onClose={onClose}>
      <form
        className="stack"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          try {
            await onSave(node);
          } catch (error) {
            notice((error as Error).message, true);
          } finally {
            setBusy(false);
          }
        }}
      >
        <Field label="标题">
          <input
            required
            value={node.title}
            onChange={(e) => setNode({ ...node, title: e.target.value })}
          />
        </Field>
        <Field label="上级目录">
          <select
            value={node.parent_id || ""}
            disabled={node.is_root}
            onChange={(e) =>
              setNode({ ...node, parent_id: e.target.value || null })
            }
          >
            <option value="">无上级</option>
            {orderNodes(nodes)
              .filter((x) => x.node.id !== node.id)
              .map(({ node: n, depth }) => (
                <option value={n.id} key={n.id}>
                  {"　".repeat(depth)}
                  {n.title}
                </option>
              ))}
          </select>
        </Field>
        <Field label="同级顺序">
          <input
            type="number"
            value={node.order}
            onChange={(e) =>
              setNode({ ...node, order: Number(e.target.value) })
            }
          />
        </Field>
        <div className="actions">
          <Button onClick={onClose}>取消</Button>
          <Button kind="primary" busy={busy} type="submit">
            保存校正
          </Button>
        </div>
      </form>
    </Modal>
  );
}
function BlockEditor({
  block: initial,
  nodes,
  bookId,
  onClose,
  onSave,
  onArchive,
  onRestore,
}: {
  block: Block;
  nodes: Node[];
  bookId: string;
  onClose: () => void;
  onSave: (block: Block) => Promise<void>;
  onArchive: () => Promise<void>;
  onRestore: (revision: number) => Promise<void>;
}) {
  const [block, setBlock] = useState(initial),
    [busy, setBusy] = useState(false),
    [history, setHistory] = useState<Block[] | null>(null);
  const notice = useNotice();
  return (
    <Modal title="编辑教材段落" onClose={onClose} wide>
      <form
        className="stack"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          try {
            await onSave(block);
          } catch (error) {
            notice((error as Error).message, true);
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="form-grid">
          <Field label="所属目录">
            <select
              value={block.node_id || ""}
              onChange={(e) =>
                setBlock({ ...block, node_id: e.target.value || null })
              }
            >
              <option value="">未归类</option>
              {orderNodes(nodes).map(({ node, depth }) => (
                <option key={node.id} value={node.id}>
                  {"　".repeat(depth)}
                  {node.title}
                </option>
              ))}
            </select>
          </Field>
          <Field label="内容类型">
            <select
              value={block.type}
              onChange={(e) => setBlock({ ...block, type: e.target.value })}
            >
              <option value="paragraph">正文</option>
              <option value="figure">插图说明</option>
              <option value="example">例题</option>
              <option value="aside">提示 / 补充知识</option>
            </select>
          </Field>
        </div>
        <Field label="段落内容">
          <textarea
            rows={12}
            value={block.text}
            onChange={(e) => setBlock({ ...block, text: e.target.value })}
          />
        </Field>
        <Field label="段落顺序">
          <input
            type="number"
            value={block.order}
            onChange={(e) =>
              setBlock({ ...block, order: Number(e.target.value) })
            }
          />
        </Field>
        <div className="row-between">
          <div className="inline-actions">
            {block.id && (
              <>
                <Button
                  onClick={async () => {
                    try {
                      setHistory(
                        await api<Block[]>(
                          `/books/${bookId}/blocks/${block.id}/history`,
                        ),
                      );
                    } catch (e) {
                      notice((e as Error).message, true);
                    }
                  }}
                >
                  修改历史
                </Button>
                <Button
                  kind="danger"
                  onClick={async () => {
                    if (!window.confirm("确定归档此段落吗？")) return;
                    try {
                      await onArchive();
                    } catch (e) {
                      notice((e as Error).message, true);
                    }
                  }}
                >
                  归档
                </Button>
              </>
            )}
          </div>
          <Button kind="primary" busy={busy} type="submit">
            保存内容
          </Button>
        </div>
      </form>
      {history && (
        <div className="history">
          <h3>修改历史</h3>
          {history.map((b, i) => (
            <div key={i}>
              <span className="hint">修订 {b.revision}</span>
              <MathText text={b.text} />
              <Button
                disabled={busy || b.revision === block.revision}
                onClick={async () => {
                  setBusy(true);
                  try {
                    await onRestore(b.revision);
                  } catch (error) {
                    const message = (error as Error).message;
                    notice(
                      message.includes("restore_requires_topology_group")
                        ? "此版本涉及段落合并或拆分，需要整组恢复，无法单独恢复这一段。"
                        : message,
                      true,
                    );
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                恢复此版本
              </Button>
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}
function PageEditor({
  page,
  index,
  bookId,
  onChanged,
  onSchedule,
}: {
  page: Page;
  index: number;
  bookId: string;
  onChanged: () => void;
  onSchedule: (path: string) => void;
}) {
  const [text, setText] = useState(page.text || ""),
    [busy, setBusy] = useState(false);
  const notice = useNotice();
  const imageId =
    page.image_asset?.id || page.image_asset_id || page.source_asset_id;
  async function action(name: string) {
    setBusy(true);
    try {
      await post(`/books/${bookId}/pages/${page.id}/${name}`, {
        text,
        revision: page.revision,
      });
      onChanged();
      notice("页面处理方式已更新。");
    } catch (e) {
      notice((e as Error).message, true);
    } finally {
      setBusy(false);
    }
  }
  return (
    <details className="page-editor">
      <summary>
        <span>原页 {index + 1}</span>
        <Badge status={page.status} />
      </summary>
      <div className="page-compare">
        {imageId && (
          <a href={assetUrl(imageId, false)} target="_blank" rel="noreferrer">
            <img
              loading="lazy"
              src={assetUrl(imageId)}
              alt={`原页 ${index + 1}`}
            />
          </a>
        )}
        <div className="stack">
          {page.error && <p className="inline-error">{page.error}</p>}
          <Field label="本页提取内容">
            <textarea
              rows={12}
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
          </Field>
          <div className="inline-actions wrap">
            <Button
              busy={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await put(`/books/${bookId}/pages/${page.id}`, {
                    text,
                    revision: page.revision,
                  });
                  onChanged();
                  notice("本页草稿已保存。");
                } catch (e) {
                  notice((e as Error).message, true);
                } finally {
                  setBusy(false);
                }
              }}
            >
              保存草稿
            </Button>
            <Button
              onClick={() =>
                onSchedule(`/books/${bookId}/pages/${page.id}/recognize`)
              }
            >
              重新识别
            </Button>
            <Button onClick={() => void action("text-only")}>仅使用文本</Button>
            <Button onClick={() => void action("skip")}>跳过此页</Button>
          </div>
        </div>
      </div>
    </details>
  );
}
