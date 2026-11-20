import type {
  CarePlanDetail,
  CarePlanSummary,
  RunCreated,
  RunDetail,
  RunStatus,
  RunSummary,
  ReviewAction,
  SystemHealth,
} from "@app/api/models";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public directIpsEnabled = false,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

// BFF errors carry a JSON body of the shape {"message": "..."}; surface that
// human-readable string rather than the raw JSON so callers (e.g. inline
// alerts) can display it directly. Falls back to the raw text for non-JSON
// bodies.
function errorMessage(body: string): string {
  try {
    const parsed = JSON.parse(body);
    const messages: Record<string, string> = {
      "out-of-scope": "This run is not part of your current patient session.",
      "csrf": "Your session could not validate this request. Reload the page and try again.",
      "approval-stale": "This review has changed. Reload it before submitting again.",
      "session-required": "Launch the application from your EHR to start a session.",
    };
    const code = parsed?.detail?.error ?? parsed?.error;
    if (typeof code === "string") return messages[code] ?? "The request could not be completed.";
    if (parsed && typeof parsed.message === "string") return parsed.message;
  } catch {
    // not JSON — use the raw body as-is
  }
  return body;
}

export interface BrowserSession {
  kind: "patient" | "operator";
  subject: string;
  runIds: string[];
  csrfToken: string;
  expiresAt: number;
  directIpsEnabled: boolean;
}

let csrfToken = "";
export function clearSession() { csrfToken = ""; }

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const mutating = !["GET", "HEAD", "OPTIONS"].includes(init?.method ?? "GET");
  if (mutating && !csrfToken) {
    try { await getSession(); }
    catch (error) {
      // Direct IPS is the dev-only session entry point.
      if (!(path === "/api/v1/runs" && error instanceof ApiError && error.directIpsEnabled)) throw error;
    }
  }
  const res = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(mutating ? { "X-CSRF-Token": csrfToken } : {}), ...init?.headers },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    let directIpsEnabled = false;
    try { directIpsEnabled = JSON.parse(text).directIpsEnabled === true; } catch { /* non-JSON response */ }
    if (res.status === 401 && path !== "/api/v1/session") {
      clearSession();
      window.dispatchEvent(new Event("acp-session-expired"));
    }
    throw new ApiError(res.status, errorMessage(text) || res.statusText, directIpsEnabled);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export async function getSession(): Promise<BrowserSession> {
  const session = await request<BrowserSession>("/api/v1/session");
  csrfToken = session.csrfToken;
  return session;
}

export async function logout(): Promise<void> {
  await request<void>("/api/v1/session/logout", { method: "POST" });
  clearSession();
  window.dispatchEvent(new Event("acp-session-expired"));
}

const B = "/api/v1";

// --- Runs ---
export async function createRun(
  ipsBundle: Record<string, unknown>,
): Promise<RunCreated> {
  const run = await request<RunCreated>(`${B}/runs`, {
    method: "POST",
    body: JSON.stringify({ ipsBundle }),
  });
  // The development entry point creates/replaces the patient session cookie.
  // Update the provider before navigating into a session-protected run.
  const session = await getSession();
  window.dispatchEvent(new CustomEvent<BrowserSession>("acp-session-changed", { detail: session }));
  return run;
}

export async function listRuns(filters?: {
  status?: RunStatus;
  limit?: number;
}): Promise<RunSummary[]> {
  const params = new URLSearchParams();
  if (filters?.status) params.set("status", filters.status);
  if (filters?.limit != null) params.set("limit", String(filters.limit));
  const qs = params.toString();
  return request(`${B}/runs${qs ? `?${qs}` : ""}`);
}

export async function getRunDetail(runId: string): Promise<RunDetail> {
  return request(`${B}/runs/${runId}`);
}

export async function cancelRun(runId: string): Promise<void> {
  return request(`${B}/runs/${runId}`, { method: "DELETE" });
}

export async function submitReview(
  runId: string,
  action: ReviewAction,
): Promise<RunDetail> {
  return request(`${B}/runs/${runId}/review/careplan`, {
    method: "POST",
    body: JSON.stringify(action),
  });
}

// --- Care plans (persisted, read-only) ---
export async function listCarePlans(): Promise<CarePlanSummary[]> {
  return request(`${B}/careplans`);
}

export async function getCarePlan(id: string): Promise<CarePlanDetail> {
  return request(`${B}/careplans/${id}`);
}

// --- Status ---
export async function getSystemStatus(): Promise<SystemHealth> {
  return request(`${B}/status`);
}

export async function healthCheck(): Promise<{ status: string }> {
  return request("/health");
}
