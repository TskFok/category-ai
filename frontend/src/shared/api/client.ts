import type {
  AnalysisReport,
  AudioExport,
  AudioSegment,
  BookRenderJob,
  ChapterDetail,
  ChapterSummary,
  CharacterDetail,
  CharacterSummary,
  CoverageValidation,
  EgoGraph,
  Episode,
  EpisodePlan,
  EventItem,
  EvidenceDetail,
  EvidenceSummary,
  ExportArtifact,
  Job,
  MentionItem,
  Novel,
  NovelAudioReadiness,
  PaginatedResponse,
  ProviderUsageSummary,
  RelationshipItem,
  RenderJob,
  ReviewTask,
  ScriptSegment,
  ScriptValidation,
  UsageSummary,
  VoiceLockResult,
  VoiceProfile,
  VoiceProviderStatus,
} from "../types/api";

const API_BASE = "/api/v1";
const AUTH_TOKEN_KEY = "ncke_auth_token";

export function getAuthToken(): string {
  try {
    return sessionStorage.getItem(AUTH_TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

export function setAuthToken(token: string): void {
  try {
    const trimmed = token.trim();
    if (trimmed) {
      sessionStorage.setItem(AUTH_TOKEN_KEY, trimmed);
      // P1-08: Establish HttpOnly SameSite=Lax cookie so <audio> & downloads authenticate without URL tokens
      fetch(`${API_BASE}/auth/session`, {
        method: "POST",
        credentials: "same-origin",
        headers: { Authorization: `Bearer ${trimmed}` },
      }).catch(() => {});
    } else {
      sessionStorage.removeItem(AUTH_TOKEN_KEY);
    }
  } catch {
    // ignore storage errors
  }
}

/**
 * P1-08: Never append long-lived access_token into URLs (leaks to logs & browser history).
 * Media playback & downloads rely on the HttpOnly session cookie or short-lived signed media_token.
 */
export function getAuthorizedMediaUrl(url: string): string {
  return url;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getAuthToken();
  const authHeader: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: "same-origin",
    headers: {
      ...(init?.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...authHeader,
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

  uploadNovel: (file: File, title: string, author?: string, copyrightConfirmed = true) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("title", title);
    if (author) fd.append("author", author);
    fd.append("copyright_confirmed", String(copyrightConfirmed));
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
      character_ids?: string[];
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

  // Data Exports (NCKE)
  listExports: (novelId: string) => request<ExportArtifact[]>(`/novels/${novelId}/exports`),
  createExport: (novelId: string, format: "json" | "csv" | "graphml") =>
    request<ExportArtifact>(`/novels/${novelId}/exports`, {
      method: "POST",
      body: JSON.stringify({ format }),
    }),

  // M1: Episode Plans & Episodes
  listEpisodePlans: (novelId: string) =>
    request<EpisodePlan[]>(`/novels/${novelId}/episode-plans`),

  createEpisodePlan: (
    novelId: string,
    payload: {
      target_minutes?: number;
      chars_per_minute?: number;
      strategy?: "chapter_first" | "story_first";
      max_deviation_pct?: number;
    }
  ) =>
    request<EpisodePlan>(`/novels/${novelId}/episode-plans`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  listEpisodes: (planId: string) =>
    request<Episode[]>(`/episode-plans/${planId}/episodes`),

  updateEpisode: (
    episodeId: string,
    payload: Partial<Pick<Episode, "title" | "rationale" | "start_offset" | "end_offset" | "status">>
  ) =>
    request<Episode>(`/episodes/${episodeId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),

  validateEpisodePlan: (planId: string) =>
    request<CoverageValidation>(`/episode-plans/${planId}/validate`, { method: "POST" }),

  // M1: Dialogue Scripts
  analyzeEpisodeScript: (episodeId: string) =>
    request<ScriptSegment[]>(`/episodes/${episodeId}/analyze-script`, { method: "POST" }),

  validateEpisodeScript: (episodeId: string) =>
    request<ScriptValidation>(`/episodes/${episodeId}/validate-script`, { method: "POST" }),

  listEpisodeSegments: (episodeId: string, reviewStatus?: string) => {
    const qs = new URLSearchParams();
    if (reviewStatus) qs.set("review_status", reviewStatus);
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<ScriptSegment[]>(`/episodes/${episodeId}/segments${suffix}`);
  },

  updateScriptSegment: (
    segmentId: string,
    payload: {
      segment_type?: string;
      speaker_character_id?: string | null;
      set_narrator?: boolean;
      narrator_key?: string;
      emotion?: string;
      emotion_intensity?: number;
      speech_rate?: number;
      review_status?: string;
    }
  ) =>
    request<ScriptSegment>(`/script-segments/${segmentId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),

  // M2: Voice Profiles
  listVoiceProfiles: (novelId: string) =>
    request<VoiceProfile[]>(`/novels/${novelId}/voice-profiles`),

  createVoiceProfile: (
    novelId: string,
    payload: {
      character_id?: string | null;
      narrator_key?: string | null;
      display_name?: string;
      voice_prompt?: string;
      voice_id?: string;
      consent_confirmed?: boolean;
    }
  ) =>
    request<VoiceProfile>(`/novels/${novelId}/voice-profiles`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  designVoiceCandidates: (profileId: string, previewText?: string) =>
    request<VoiceProfile>(`/voice-profiles/${profileId}/design-candidates`, {
      method: "POST",
      body: JSON.stringify({ preview_text: previewText }),
    }),

  lockVoiceProfile: (
    profileId: string,
    payload: {
      candidate_index?: number;
      voice_id?: string;
      voice_prompt?: string;
      scope?: "unrendered_only" | "current_episode" | "all_episodes";
    }
  ) =>
    request<VoiceLockResult>(`/voice-profiles/${profileId}/lock`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  updateVoiceAndInvalidate: (
    profileId: string,
    payload: {
      voice_id?: string;
      voice_prompt?: string;
      default_rate?: number;
      pitch?: number;
      scope?: "unrendered_only" | "current_episode" | "all_episodes";
    }
  ) =>
    request<VoiceLockResult>(`/voice-profiles/${profileId}/update-and-invalidate`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  getVoiceProviderStatus: (profileId: string) =>
    request<VoiceProviderStatus>(`/voice-profiles/${profileId}/provider-status`),

  // M3/M4: Render Jobs, Book Render Jobs & Audio Exports
  createRenderJob: (
    episodeId: string,
    payload: { budget_limit?: number; idempotency_key?: string; run_inline?: boolean } = {
      run_inline: false,
    }
  ) =>
    request<RenderJob>(`/episodes/${episodeId}/render-jobs`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  listRenderJobs: (episodeId: string) =>
    request<RenderJob[]>(`/episodes/${episodeId}/render-jobs`),

  pauseRenderJob: (jobId: string) =>
    request<RenderJob>(`/render-jobs/${jobId}/pause`, { method: "POST" }),

  resumeRenderJob: (jobId: string) =>
    request<RenderJob>(`/render-jobs/${jobId}/resume`, { method: "POST" }),

  cancelRenderJob: (jobId: string) =>
    request<RenderJob>(`/render-jobs/${jobId}/cancel`, { method: "POST" }),

  retryFailedRenderJob: (jobId: string) =>
    request<RenderJob>(`/render-jobs/${jobId}/retry-failed`, { method: "POST" }),

  createBookRenderJob: (
    planId: string,
    payload: { episode_ids?: string[]; budget_limit?: number; run_inline?: boolean } = {
      run_inline: false,
    }
  ) =>
    request<BookRenderJob>(`/episode-plans/${planId}/render`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  getBookRenderJob: (jobId: string) =>
    request<BookRenderJob>(`/book-render-jobs/${jobId}`),

  getNovelAudioReadiness: (novelId: string, planId?: string) => {
    const qs = planId ? `?plan_id=${encodeURIComponent(planId)}` : "";
    return request<NovelAudioReadiness>(`/novels/${novelId}/audio-readiness${qs}`);
  },

  regenerateScriptSegment: (segmentId: string) =>
    request<AudioSegment>(`/script-segments/${segmentId}/regenerate`, { method: "POST" }),

  listAudioExports: (novelId: string) =>
    request<AudioExport[]>(`/novels/${novelId}/audio-exports`),

  createAudioExport: (
    novelId: string,
    payload: {
      episode_id?: string;
      plan_id?: string;
      format: "mp3" | "wav" | "zip";
      allow_wav_fallback?: boolean;
    }
  ) =>
    request<AudioExport>(`/novels/${novelId}/audio-exports`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  getProviderUsage: (novelId: string) =>
    request<ProviderUsageSummary>(`/novels/${novelId}/provider-usage`),
};

/**
 * FIX-04: Subscribe to Job SSE stream using authenticated fetch streaming
 * (supports Authorization: Bearer header without leaking secrets to URLs).
 */
export function subscribeJobEvents(
  jobId: string,
  onUpdate: (data: Record<string, unknown>) => void
): () => void {
  const controller = new AbortController();
  const seenIds = new Set<string>();
  const token = getAuthToken();

  (async () => {
    try {
      const res = await fetch(`${API_BASE}/jobs/${jobId}/events`, {
        method: "GET",
        credentials: "same-origin",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        signal: controller.signal,
      });
      if (!res.ok || !res.body) return;

      const reader = res.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const blocks = buffer.split("\n\n");
        buffer = blocks.pop() || "";

        for (const block of blocks) {
          let evtId = "";
          let dataLine = "";
          for (const line of block.split("\n")) {
            if (line.startsWith("id:")) evtId = line.slice(3).trim();
            if (line.startsWith("data:")) dataLine += line.slice(5).trim();
          }
          if (evtId && seenIds.has(evtId)) continue;
          if (evtId) seenIds.add(evtId);
          if (dataLine) {
            try {
              onUpdate(JSON.parse(dataLine));
            } catch {
              // ignore malformed JSON
            }
          }
        }
      }
    } catch {
      // stream aborted or closed
    }
  })();

  return () => controller.abort();
}
