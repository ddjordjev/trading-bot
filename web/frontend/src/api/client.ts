const TOKEN = localStorage.getItem("dashboard_token") || "";

let _baseUrl = "";
let _wsBase = "";

export function setBotPort(port: number | null) {
  if (!port || port === Number(window.location.port)) {
    _baseUrl = "";
    _wsBase = "";
  } else {
    _baseUrl = `${window.location.protocol}//${window.location.hostname}:${port}`;
    const wProto = window.location.protocol === "https:" ? "wss:" : "ws:";
    _wsBase = `${wProto}//${window.location.hostname}:${port}`;
  }
}

function headers(): HeadersInit {
  const h: HeadersInit = { "Content-Type": "application/json" };
  if (TOKEN) h["Authorization"] = `Bearer ${TOKEN}`;
  return h;
}

async function api<T>(path: string, opts?: RequestInit): Promise<T> {
  const url = _baseUrl ? `${_baseUrl}${path}` : path;
  const res = await fetch(url, { headers: headers(), ...opts });
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
  if (_wsBase) {
    return `${_wsBase}/ws?token=${token}`;
  }
  return `${proto}//${window.location.host}/ws?token=${token}`;
}

export function wsUrlForPort(port: number): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const token = localStorage.getItem("dashboard_token") || "";
  return `${proto}//${window.location.hostname}:${port}/ws?token=${token}`;
}

export function apiUrlForPort(port: number, path: string): string {
  return `${window.location.protocol}//${window.location.hostname}:${port}${path}`;
}
