import { ApiError, CacheStats, Capabilities, ContextInspector, CuratedMemory, Evidence, EvolutionOverview, EvolutionReviewDetail, EvolutionReviewSummary, MemoryCapacity, Report, Run, Session, SessionDetail, SourceMode, WhiteboardLog, WorkflowMode } from "../types/api";

export const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000/api/v1").replace(/\/$/, "");

// ---- 租户凭据（X-API-Key）----------------------------------------------
// 后端签发过 api_keys 后强制认证（见 backend/app/tenancy.py）；key 只存
// localStorage，随每个请求头注入。401 统一广播给设置弹窗重新采集。

const API_KEY_STORAGE = "dra.api_key";
const unauthorizedListeners = new Set<() => void>();

export function getApiKey(): string {
  return localStorage.getItem(API_KEY_STORAGE) ?? "";
}

export function setApiKey(value: string): void {
  const trimmed = value.trim();
  if (trimmed) localStorage.setItem(API_KEY_STORAGE, trimmed);
  else localStorage.removeItem(API_KEY_STORAGE);
}

export function clearApiKey(): void {
  localStorage.removeItem(API_KEY_STORAGE);
}

/** 订阅 401 事件（任意请求被拒时触发）；返回退订函数。 */
export function onUnauthorized(listener: () => void): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

function authHeaders(): Record<string, string> {
  const key = getApiKey();
  return key ? { "X-API-Key": key } : {};
}

// SSE 流式请求（useRunEvents）不能走 request<T>（响应是流不是 JSON），
// 单独导出请求头构造器
export { authHeaders };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers: { "Content-Type": "application/json", ...authHeaders(), ...init?.headers } });
  if (!response.ok) {
    if (response.status === 401) for (const listener of [...unauthorizedListeners]) listener();
    const payload = await response.json().catch(() => ({ error: { code: "NETWORK_ERROR", message: response.statusText } }));
    throw new ApiError(payload.error ?? { code: "HTTP_ERROR", message: response.statusText });
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  capabilities: () => request<Capabilities>("/capabilities"),
  health: () => request<{status: string; checked_at: string; components: Record<string, {status: string; configured?: boolean; reason?: string | null; check_level?: string}>}>("/health"),
  cacheStats: () => request<CacheStats>("/cache/stats"),
  sessions: (limit = 30, offset = 0) => request<{items: Session[]; total: number; next_offset?: number | null}>(`/sessions?include_archived=true&limit=${limit}&offset=${offset}`),
  session: (id: string) => request<SessionDetail>(`/sessions/${id}`),
  whiteboard: (id: string) => request<WhiteboardLog>(`/sessions/${id}/whiteboard`),
  createSession: (title = "新对话") => request<Session>("/sessions", { method: "POST", body: JSON.stringify({ title }) }),
  patchSession: (id: string, payload: {title?: string; status?: string}) => request<Session>(`/sessions/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteSession: (id: string) => request<void>(`/sessions/${id}`, { method: "DELETE" }),
  send: (sessionId: string, payload: {client_message_id: string; content: string; source_mode: SourceMode; workflow_mode: WorkflowMode; sources?: SourceMode[]}) =>
    request<{message_id: string; run_id: string; status: string; events_url: string}>(`/sessions/${sessionId}/messages`, { method: "POST", body: JSON.stringify(payload) }),
  run: (id: string) => request<Run>(`/runs/${id}`),
  cancel: (id: string) => request(`/runs/${id}/cancel`, { method: "POST" }),
  pause: (id: string) => request(`/runs/${id}/pause`, { method: "POST" }),
  resume: (id: string) => request(`/runs/${id}/resume`, { method: "POST" }),
  clarify: (id: string, content: string, clientClarificationId?: string, questionId?: string) =>
    request(`/runs/${id}/clarifications`, { method: "POST", body: JSON.stringify({ content, client_clarification_id: clientClarificationId, question_id: questionId }) }),
  evidence: (id: string) => request<{items: Evidence[]; total: number}>(`/runs/${id}/evidence`),
  report: (id: string) => request<Report>(`/runs/${id}/report`),
  downloadReport: async (id: string) => {
    const response = await fetch(`${API_BASE}/runs/${id}/report/download`, { headers: authHeaders() });
    if (!response.ok) {
      if (response.status === 401) for (const listener of [...unauthorizedListeners]) listener();
      const payload = await response.json().catch(() => ({ error: { code: "HTTP_ERROR", message: response.statusText } }));
      throw new ApiError(payload.error ?? { code: "HTTP_ERROR", message: response.statusText });
    }
    return response.blob();
  },
  context: (id: string) => request<ContextInspector>(`/runs/${id}/context`),
  memories: () => request<{items: CuratedMemory[]; total: number}>("/memories"),
  memoryCapacity: () => request<{targets: Record<"user" | "project", MemoryCapacity>}>("/memories/capacity"),
  createMemory: (payload: Record<string, unknown>) => request<CuratedMemory>("/memories", { method: "POST", body: JSON.stringify(payload) }),
  patchMemory: (id: string, payload: Record<string, unknown>) => request<CuratedMemory>(`/memories/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteMemory: (id: string) => request<void>(`/memories/${id}`, { method: "DELETE" }),
  skills: () => request<{versions: Array<Record<string, unknown>>; candidates: Array<Record<string, unknown>>}>("/skills"),
  skill: (name: string, version: string) => request<Record<string, unknown>>(`/skills/${encodeURIComponent(name)}/versions/${encodeURIComponent(version)}`),
  skillCandidate: (id: string) => request<Record<string, unknown>>(`/skills/candidates/${encodeURIComponent(id)}`),
  evaluateSkillCandidate: (id: string) => request<Record<string, unknown>>(`/skills/candidates/${encodeURIComponent(id)}/evaluate`, {method: "POST"}),
  promoteSkillCandidate: (id: string, confirmation: string) => request<Record<string, unknown>>(`/skills/candidates/${encodeURIComponent(id)}/promote`, {method: "POST", body: JSON.stringify({confirmation})}),
  evaluateSkill: (name: string, version: string) => request<Record<string, unknown>>(`/skills/${encodeURIComponent(name)}/versions/${encodeURIComponent(version)}/evaluate`, {method: "POST"}),
  promoteSkill: (name: string, version: string, confirmation: string) => request<Record<string, unknown>>(`/skills/${encodeURIComponent(name)}/versions/${encodeURIComponent(version)}/promote`, {method: "POST", body: JSON.stringify({confirmation})}),
  deploySkill: (name: string, version: string, targetStage: string, confirmation: string) => request<Record<string, unknown>>(`/skills/${encodeURIComponent(name)}/versions/${encodeURIComponent(version)}/deploy`, {method: "POST", body: JSON.stringify({target_stage: targetStage, confirmation})}),
  suspendSkill: (name: string, version: string, reason: string, confirmation: string) => request<Record<string, unknown>>(`/skills/${encodeURIComponent(name)}/versions/${encodeURIComponent(version)}/suspend`, {method: "POST", body: JSON.stringify({reason, confirmation})}),
  learningReview: (id: string) => request<Record<string, unknown>>(`/learning-reviews/${encodeURIComponent(id)}`),
  evolutionOverview: () => request<EvolutionOverview>("/evolution/overview"),
  evolutionReviews: (status = "", q = "") => request<{items: EvolutionReviewSummary[]; total: number}>(`/evolution/reviews?status=${encodeURIComponent(status)}&q=${encodeURIComponent(q)}`),
  evolutionReview: (id: string) => request<EvolutionReviewDetail>(`/evolution/reviews/${encodeURIComponent(id)}`),
  retryLearningReview: (id: string) => request<Record<string, unknown>>(`/learning-reviews/${encodeURIComponent(id)}/retry`, {method: "POST"}),
  rollbackSkill: (name: string) => request<Record<string, unknown>>(`/skills/${encodeURIComponent(name)}/rollback`, {method: "POST"}),
};
