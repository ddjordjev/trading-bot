function headers(): HeadersInit {
  const h: HeadersInit = { "Content-Type": "application/json" };
  const token = localStorage.getItem("dashboard_token") || "";
  if (token) h["Authorization"] = `Bearer ${token}`;
  return h;
}

async function api<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(path, { headers: headers(), ...opts });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status}: ${body}`);
  }
  return res.json();
}

export const get = <T>(path: string) => api<T>(path);
export const post = <T>(path: string) => api<T>(path, { method: "POST" });
export const postBody = <T>(path: string, body: object) =>
  api<T>(path, { method: "POST", body: JSON.stringify(body) });
export const postQuery = <T>(path: string, params: Record<string, string | number>) => {
  const qs = new URLSearchParams(
    Object.entries(params).map(([k, v]) => [k, String(v)]),
  ).toString();
  return api<T>(`${path}?${qs}`, { method: "POST" });
};

export function setToken(token: string) {
  localStorage.setItem("dashboard_token", token);
  window.location.reload();
}

export function wsUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const token = localStorage.getItem("dashboard_token") || "";
  const params = new URLSearchParams({ token });
  return `${proto}//${window.location.host}/ws?${params.toString()}`;
}
