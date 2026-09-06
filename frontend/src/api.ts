let csrf = "";
export function setCsrf(value: string): void {
  csrf = value;
}
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
export function detailText(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail))
    return detail
      .map((x) =>
        typeof x === "object" && x && "msg" in x ? String(x.msg) : String(x),
      )
      .join("；");
  if (detail && typeof detail === "object") return JSON.stringify(detail);
  return "请求失败，请重试。";
}
export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const method = options.method || "GET";
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  if (method !== "GET" && method !== "HEAD") headers.set("X-CSRF-Token", csrf);
  const response = await fetch(`/api${path}`, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401)
      window.dispatchEvent(new Event("studyquip:unauthorized"));
    throw new ApiError(
      detailText(
        data && typeof data === "object" && "detail" in data
          ? data.detail
          : data,
      ) || response.statusText,
      response.status,
    );
  }
  if (method !== "GET" && method !== "HEAD") {
    const result = data as {
      job?: unknown;
      kind?: string;
      status?: string;
    } | null;
    window.dispatchEvent(
      new CustomEvent("studyquip:jobs-changed", {
        detail:
          result?.job || (result?.kind && result.status ? result : undefined),
      }),
    );
  }
  return data as T;
}
export const post = <T>(path: string, body: unknown = {}) =>
  api<T>(path, { method: "POST", body: JSON.stringify(body) });
export const put = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "PUT", body: JSON.stringify(body) });
export const remove = (path: string) =>
  api<unknown>(path, { method: "DELETE" });
