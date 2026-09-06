import { useEffect, useRef, useState } from "react";
import { api, post } from "./api";
import { assetUrl, type Asset } from "./types";
import { Button, Field, Modal, moveItem, useNotice } from "./ui";
export default function Uploads({
  ids,
  onChange,
  label = "图片",
  allowPdf = false,
  onCrop,
}: {
  ids: string[];
  onChange: (ids: string[]) => void;
  label?: string;
  allowPdf?: boolean;
  onCrop?: (id: string) => void;
}) {
  const files = useRef<HTMLInputElement>(null),
    camera = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false),
    [crop, setCrop] = useState<string | null>(null),
    [preview, setPreview] = useState<string | null>(null),
    [metadata, setMetadata] = useState<Record<string, Asset>>({});
  const notice = useNotice();
  const assetKey = ids.join(",");
  useEffect(() => {
    let active = true;
    void Promise.all(
      ids.map(async (id) => {
        try {
          const asset = await api<Asset>(`/assets/${encodeURIComponent(id)}`);
          if (active) setMetadata((current) => ({ ...current, [id]: asset }));
        } catch {
          /* 原文件入口仍可使用；读取失败时不开放裁剪。 */
        }
      }),
    );
    return () => {
      active = false;
    };
  }, [assetKey]);
  async function upload(list: FileList | null) {
    if (!list?.length) return;
    setBusy(true);
    const added: string[] = [];
    try {
      for (const file of Array.from(list)) {
        const form = new FormData();
        form.append("file", file);
        const asset = await api<Asset>("/assets", {
          method: "POST",
          body: form,
        });
        added.push(asset.id);
      }
      onChange([...ids, ...added]);
    } catch (e) {
      if (added.length) onChange([...ids, ...added]);
      notice((e as Error).message, true);
    } finally {
      setBusy(false);
      if (files.current) files.current.value = "";
      if (camera.current) camera.current.value = "";
    }
  }
  return (
    <div className="upload-field">
      <div className="row-between">
        <span className="field-label">{label}</span>
        <div className="inline-actions">
          <Button busy={busy} onClick={() => files.current?.click()}>
            {allowPdf ? "选择文件" : "相册 / 文件"}
          </Button>
          <Button disabled={busy} onClick={() => camera.current?.click()}>
            拍照
          </Button>
        </div>
      </div>
      <input
        ref={files}
        hidden
        type="file"
        multiple
        accept={
          allowPdf ? "image/*,application/pdf,text/plain,.txt" : "image/*"
        }
        onChange={(e) => void upload(e.target.files)}
      />
      <input
        ref={camera}
        hidden
        type="file"
        accept="image/*"
        capture="environment"
        onChange={(e) => void upload(e.target.files)}
      />
      {ids.length > 0 && (
        <div className="asset-list">
          {ids.map((id, i) => (
            <div className="asset-item" key={`${id}-${i}`}>
              <button
                type="button"
                className="asset-preview"
                onClick={() => setPreview(id)}
              >
                {metadata[id]?.mime.startsWith("image/") && (
                  <img
                    src={assetUrl(id)}
                    alt={`${label} ${i + 1}`}
                    onError={(e) => {
                      e.currentTarget.style.display = "none";
                    }}
                  />
                )}
                <span>{i + 1}</span>
                {!metadata[id]?.mime.startsWith("image/") && (
                  <span className="asset-file-name">
                    {metadata[id]?.name || "文件"}
                  </span>
                )}
              </button>
              <div className="asset-tools">
                <Button
                  aria-label={`第${i + 1}张上移`}
                  disabled={i === 0}
                  onClick={() => onChange(moveItem(ids, i, -1))}
                >
                  ↑
                </Button>
                <Button
                  aria-label={`第${i + 1}张下移`}
                  disabled={i === ids.length - 1}
                  onClick={() => onChange(moveItem(ids, i, 1))}
                >
                  ↓
                </Button>
                {metadata[id]?.mime.startsWith("image/") && (
                  <Button onClick={() => setCrop(id)}>裁剪</Button>
                )}
                <Button onClick={() => onChange(ids.filter((_, n) => n !== i))}>
                  移除
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}
      {crop && (
        <Crop
          id={crop}
          onClose={() => setCrop(null)}
          onDone={(id) => {
            if (onCrop) onCrop(id);
            else onChange(ids.map((x) => (x === crop ? id : x)));
            setCrop(null);
            notice(onCrop ? "已添加裁剪配图。" : "已替换为裁剪后的图片。");
          }}
        />
      )}
      {preview && (
        <Modal title="原始材料" onClose={() => setPreview(null)} wide>
          {metadata[preview]?.mime.startsWith("image/") && (
            <img
              className="full-preview"
              src={assetUrl(preview, false)}
              alt="原始材料"
            />
          )}
          <a
            className="text-link"
            target="_blank"
            rel="noreferrer"
            href={assetUrl(preview, false)}
          >
            在新页面查看原文件
          </a>
        </Modal>
      )}
    </div>
  );
}
function Crop({
  id,
  onDone,
  onClose,
}: {
  id: string;
  onDone: (id: string) => void;
  onClose: () => void;
}) {
  const img = useRef<HTMLImageElement>(null);
  const [box, setBox] = useState<[number, number, number, number]>([
      0, 0, 100, 100,
    ]),
    [start, setStart] = useState<[number, number] | null>(null),
    [busy, setBusy] = useState(false),
    [failed, setFailed] = useState(false);
  const notice = useNotice();
  function point(e: React.PointerEvent): [number, number] {
    const bounds = e.currentTarget.getBoundingClientRect();
    return [
      Math.max(
        0,
        Math.min(100, ((e.clientX - bounds.left) / bounds.width) * 100),
      ),
      Math.max(
        0,
        Math.min(100, ((e.clientY - bounds.top) / bounds.height) * 100),
      ),
    ];
  }
  async function submit() {
    if (!img.current?.naturalWidth) return;
    const [x1, y1, x2, y2] = box;
    if (x2 - x1 < 1 || y2 - y1 < 1) {
      notice("请选取有效裁剪区域。", true);
      return;
    }
    setBusy(true);
    try {
      const width = img.current.naturalWidth,
        height = img.current.naturalHeight;
      const value = await post<Asset>(`/assets/${id}/crop`, {
        box: [
          (x1 / 100) * width,
          (y1 / 100) * height,
          (x2 / 100) * width,
          (y2 / 100) * height,
        ].map(Math.round),
      });
      onDone(value.id);
    } catch (e) {
      notice((e as Error).message, true);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="裁剪图片" onClose={onClose} wide>
      <p className="hint">
        拖动选取需要保留的区域，也可以调整下方百分比。原始图片会保留。
      </p>
      <div
        className="crop-surface"
        onPointerDown={(e) => {
          e.currentTarget.setPointerCapture(e.pointerId);
          const p = point(e);
          setStart(p);
          setBox([p[0], p[1], p[0], p[1]]);
        }}
        onPointerMove={(e) => {
          if (start) {
            const p = point(e);
            setBox([
              Math.min(start[0], p[0]),
              Math.min(start[1], p[1]),
              Math.max(start[0], p[0]),
              Math.max(start[1], p[1]),
            ]);
          }
        }}
        onPointerUp={() => setStart(null)}
        onPointerCancel={() => setStart(null)}
      >
        <img
          ref={img}
          src={assetUrl(id)}
          alt="拖动选取裁剪区域"
          onError={() => setFailed(true)}
          draggable={false}
        />
        <div
          className="crop-box"
          style={{
            left: `${box[0]}%`,
            top: `${box[1]}%`,
            width: `${box[2] - box[0]}%`,
            height: `${box[3] - box[1]}%`,
          }}
        />
      </div>
      {failed && <p className="inline-error">此文件无法作为图片裁剪。</p>}
      <div className="form-grid four">
        {["左侧 %", "顶部 %", "右侧 %", "底部 %"].map((label, i) => (
          <Field label={label} key={label}>
            <input
              type="number"
              min="0"
              max="100"
              value={Math.round(box[i])}
              onChange={(e) =>
                setBox(
                  box.map((n, j) =>
                    i === j ? Number(e.target.value) : n,
                  ) as typeof box,
                )
              }
            />
          </Field>
        ))}
      </div>
      <div className="actions">
        <Button onClick={onClose}>取消</Button>
        <Button kind="primary" disabled={failed} busy={busy} onClick={submit}>
          保存裁剪
        </Button>
      </div>
    </Modal>
  );
}
