export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000/api/v1").replace(
  /\/$/,
  "",
);

export function accessToken() {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem("apw_access_token");
}

export async function apiRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (!(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const token = accessToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers, cache: "no-store" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    // A token that expires mid-session would otherwise surface as a generic
    // error on every page while the mirror cookie still reads as logged in.
    // Drop the session centrally so one expiry ends up at /login, not stuck.
    if (response.status === 401 && typeof window !== "undefined") {
      clearSession();
      window.location.replace("/login");
    }
    const detail = payload?.detail ?? payload?.error;
    const message = typeof detail === "string" ? detail : detail?.message;
    throw new Error(message || `请求失败（${response.status}）`);
  }
  return (payload?.data ?? payload) as T;
}

export function pagedItems<T>(payload: unknown): T[] {
  if (Array.isArray(payload)) return payload as T[];
  if (payload && typeof payload === "object" && Array.isArray((payload as { items?: unknown }).items))
    return (payload as { items: T[] }).items;
  return [];
}

// Read the JWT's own exp claim so the mirror cookie cannot outlive the token it
// stands for. A cookie that survives its token makes the middleware serve
// workspace pages the shell then has to bounce, which deadlocks both sides.
function tokenLifetimeSeconds(token: string): number {
  const claims = token.split(".")[1];
  if (!claims) return 0;
  try {
    const decoded: unknown = JSON.parse(atob(claims.replace(/-/g, "+").replace(/_/g, "/")));
    const exp = (decoded as { exp?: unknown })?.exp;
    if (typeof exp !== "number") return 0;
    return Math.max(0, Math.floor(exp - Date.now() / 1000));
  } catch {
    return 0;
  }
}

export function saveSession(payload: { access_token: string }) {
  window.localStorage.setItem("apw_access_token", payload.access_token);
  // Mirror the session into a cookie so the server-side middleware can reject
  // unauthenticated page requests before any business UI is served (BUG-001).
  const maxAge = tokenLifetimeSeconds(payload.access_token);
  if (maxAge <= 0) {
    clearSession();
    throw new Error("登录令牌无效或已过期，请重新登录。");
  }
  document.cookie = `apw_session=1; path=/; max-age=${maxAge}; samesite=lax`;
}

export function clearSession() {
  window.localStorage.removeItem("apw_access_token");
  document.cookie = "apw_session=; path=/; max-age=0; samesite=lax";
}
