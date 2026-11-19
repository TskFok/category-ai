import type {
  AnalysisReport,
  ChapterDetail,
  ChapterSummary,
  CharacterDetail,
  CharacterSummary,
  EgoGraph,
  EventItem,
  EvidenceDetail,
  EvidenceSummary,
  ExportArtifact,
  Job,
  MentionItem,
  Novel,
  PaginatedResponse,
  RelationshipItem,
  ReviewTask,
  UsageSummary,
} from "../types/api";

const API_BASE = "/api/v1";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...(init?.headers || {}),
    },
  });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const err = await res.json();
      msg = err.message || err.detail?.message || msg;
    } catch {
      // ignore
    }
    throw new Error(msg);
  }
  return res.json() as Promise<T>;
}

export const api = {
  // Health
  healthReady: () => request<{ status: string; checks: Record<string, string> }>("/health/ready"),

  // Novels & Chapters
  listNovels: (page = 1, pageSize = 20) =>
    request<PaginatedResponse<Novel>>(`/novels?page=${page}&page_size=${pageSize}`),

  uploadNovel: (file: File, title: string, author?: string) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("title", title);
    if (author) fd.append("author", author);
    return request<Novel>("/novels", { method: "POST", body: fd });
  },

  listChapters: (novelId: string, page = 1, pageSize = 50, search?: string) => {
    const params = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
    });
    if (search) params.set("search", search);
    return request<PaginatedResponse<ChapterSummary>>(
      `/novels/${novelId}/chapters?${params.toString()}`
    );
  },

  getChapter: (chapterId: string) => request<ChapterDetail>(`/chapters/${chapterId}`),

  // Jobs & Usage
  listJobs: (novelId: string) => request<PaginatedResponse<Job>>(`/novels/${novelId}/jobs`),

  createJob: (
    novelId: string,
    payload: {
      job_type: string;
      budget_limit?: number;
      chapter_from?: number;
      chapter_to?: number;
      run_inline?: boolean;
    }
  ) =>
    request<Job>(`/novels/${novelId}/jobs`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  pauseJob: (jobId: string) => request<Job>(`/jobs/${jobId}/pause`, { method: "POST" }),
  resumeJob: (jobId: string) => request<Job>(`/jobs/${jobId}/resume`, { method: "POST" }),
  getUsage: (novelId: string) => request<UsageSummary>(`/novels/${novelId}/usage`),

  // Characters
  listCharacters: (
    novelId: string,
    params: { q?: string; status?: string; page?: number; pageSize?: number } = {}
  ) => {
    const qs = new URLSearchParams({
      page: String(params.page ?? 1),
      page_size: String(params.pageSize ?? 20),
    });
    if (params.q) qs.set("q", params.q);
    if (params.status) qs.set("status", params.status);
    return request<PaginatedResponse<CharacterSummary>>(
      `/novels/${novelId}/characters?${qs.toString()}`
    );
  },

  getCharacter: (id: string) => request<CharacterDetail>(`/characters/${id}`),
  getCharacterMentions: (id: string, page = 1) =>
    request<PaginatedResponse<MentionItem>>(`/characters/${id}/mentions?page=${page}&page_size=30`),
  getCharacterEvents: (id: string) => request<EventItem[]>(`/characters/${id}/events`),
  getCharacterRelationships: (id: string) =>
    request<RelationshipItem[]>(`/characters/${id}/relationships`),
  getCharacterAnalyses: (id: string) =>
    request<AnalysisReport[]>(`/characters/${id}/analyses`),

  // Graph
  getGraphNeighbors: (params: {
    center_character_id: string;
    depth?: number;
    relation_types?: string;
    chapter_from?: number;
    chapter_to?: number;
    review_status?: string;
  }) => {
    const qs = new URLSearchParams({
      center_character_id: params.center_character_id,
      depth: String(params.depth ?? 1),
    });
    if (params.relation_types) qs.set("relation_types", params.relation_types);
    if (params.chapter_from) qs.set("chapter_from", String(params.chapter_from));
    if (params.chapter_to) qs.set("chapter_to", String(params.chapter_to));
    if (params.review_status) qs.set("review_status", params.review_status);
    return request<EgoGraph>(`/graph/neighbors?${qs.toString()}`);
  },

  getRelationshipEvidence: (relationshipId: string) =>
    request<EvidenceSummary[]>(`/relationships/${relationshipId}/evidence`),

  // Evidence
  getEvidence: (evidenceId: string) => request<EvidenceDetail>(`/evidence/${evidenceId}`),

  // Reviews & Identity
  listReviews: (novelId: string, state?: string) => {
    const qs = new URLSearchParams({ novel_id: novelId });
    if (state) qs.set("state", state);
    return request<PaginatedResponse<ReviewTask>>(`/reviews?${qs.toString()}`);
  },

  submitReviewDecision: (reviewId: string, decision: "approve" | "reject" | "resolve", note?: string) =>
    request<ReviewTask>(`/reviews/${reviewId}/decisions`, {
      method: "POST",
      body: JSON.stringify({ decision, note }),
    }),

  mergeCharacters: (sourceId: string, targetId: string, reason: string) =>
    request("/identities/merge", {
      method: "POST",
      body: JSON.stringify({
        source_character_id: sourceId,
        target_character_id: targetId,
        reason,
      }),
    }),

  splitCharacter: (sourceId: string, newName: string, reason: string) =>
    request("/identities/split", {
      method: "POST",
      body: JSON.stringify({
        source_character_id: sourceId,
        new_canonical_name: newName,
        mention_ids: [],
        reason,
      }),
    }),

  // Exports
  listExports: (novelId: string) => request<ExportArtifact[]>(`/novels/${novelId}/exports`),
  createExport: (novelId: string, format: "json" | "csv" | "graphml") =>
    request<ExportArtifact>(`/novels/${novelId}/exports`, {
      method: "POST",
      body: JSON.stringify({ format }),
    }),
};

/**
 * Subscribe to Job SSE stream with event_id deduplication.
 */
export function subscribeJobEvents(
  jobId: string,
  onUpdate: (data: Record<string, unknown>) => void
): () => void {
  const seenIds = new Set<string>();
  const es = new EventSource(`${API_BASE}/jobs/${jobId}/events`);
  es.addEventListener("job_update", (evt) => {
    const msg = evt as MessageEvent;
    if (msg.lastEventId && seenIds.has(msg.lastEventId)) return;
    if (msg.lastEventId) seenIds.add(msg.lastEventId);
    try {
      onUpdate(JSON.parse(msg.data));
    } catch {
      // ignore malformed event
    }
  });
  es.onerror = () => {
    es.close();
  };
  return () => es.close();
}
