// Lightweight API client for the backend.
//
// The JWT is kept in localStorage. This is a pragmatic choice for a homelab
// tool; for higher-security deployments an httpOnly cookie + CSRF token would be
// preferable. The API token and other secrets never reach the frontend.

const TOKEN_KEY = "pwi_token";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

let unauthorizedHandler = null;
export function setUnauthorizedHandler(fn) {
  unauthorizedHandler = fn;
}

function formatDetail(detail, fallback) {
  if (!detail) return fallback;
  if (typeof detail === "string") return detail;
  // FastAPI validation errors come back as a list of {loc, msg, ...}.
  if (Array.isArray(detail)) {
    return detail.map((e) => e.msg || JSON.stringify(e)).join("; ");
  }
  return JSON.stringify(detail);
}

async function request(path, { method = "GET", body, auth = true } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const token = getToken();
  if (auth && token) headers["Authorization"] = `Bearer ${token}`;

  let res;
  try {
    res = await fetch(`/api${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    throw new Error("Verbindung zum Server fehlgeschlagen.");
  }

  if (res.status === 401 && auth) {
    setToken(null);
    if (unauthorizedHandler) unauthorizedHandler();
    throw new Error("Sitzung abgelaufen. Bitte erneut anmelden.");
  }

  const text = await res.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }

  if (!res.ok) {
    const detail = data && typeof data === "object" ? data.detail : data;
    throw new Error(formatDetail(detail, res.statusText || "Unbekannter Fehler"));
  }
  return data;
}

function qs(params) {
  if (!params) return "";
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== null);
  if (!entries.length) return "";
  return "?" + entries.map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
}

export const api = {
  login: (username, password) =>
    request("/auth/login", { method: "POST", body: { username, password }, auth: false }),
  me: () => request("/auth/me"),
  software: () => request("/software"),
  nodes: () => request("/proxmox/nodes"),
  defaults: () => request("/proxmox/defaults"),
  storages: (params) => request(`/proxmox/storages${qs(params)}`),
  bridges: (params) => request(`/proxmox/bridges${qs(params)}`),
  templates: (params) => request(`/proxmox/templates${qs(params)}`),
  vmTemplates: (params) => request(`/proxmox/vm-templates${qs(params)}`),
  nextVmid: () => request("/proxmox/next-vmid"),
  createContainer: (payload) => request("/containers", { method: "POST", body: payload }),
  jobs: () => request("/jobs"),
  job: (id) => request(`/jobs/${id}`),
  installUpdates: (id) => request(`/jobs/${id}/install-updates`, { method: "POST" }),
  logs: (lines = 200) => request(`/logs?lines=${lines}`),
};
