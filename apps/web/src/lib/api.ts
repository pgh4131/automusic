export type Production = {
  id: string;
  song_id: string;
  song_title: string;
  version_no: number;
  parent_production_id: string | null;
  title: string;
  prompt: string;
  provider: "elevenlabs" | "demo";
  model_id: string;
  language: string;
  genre: string;
  mood: string;
  vocal: string;
  duration_seconds: number;
  lyrics_mode: "auto" | "custom";
  lyrics: string;
  status: "queued" | "planning" | "generating" | "saving" | "ready" | "failed" | "needs_review";
  error_message: string | null;
  audio_duration: number | null;
  audio_bytes: number | null;
  audio_url: string | null;
  download_url: string | null;
  created_at: string;
};

export type Song = {
  id: string;
  title: string;
  version_count: number;
  latest_version: Production;
};

export type Configuration = {
  providers: { id: string; name: string; available: boolean; description: string }[];
  similarity_available: boolean;
};

export type SimilarityMatch = {
  track_id: string; title: string; artist: string; genre: string;
  distance: number; query_start: number; query_end: number;
  reference_start: number; reference_end: number;
  source_url: string; license_url: string; audio_url: string; time_basis: string;
  melody_distance?: number; rhythm_distance?: number;
  evidence_type?: "melody_and_duration" | "melody_only";
};

export type AnalysisMode = "chroma" | "melody_rhythm";

export type Analysis = {
  id: string; production_id: string; status: "queued" | "running" | "ready" | "failed";
  created_at: string; completed_at: string | null; error_message: string | null;
  elapsed_ms: number | null; corpus_tracks: number; corpus_id: string; algorithm_version: string;
  mode: AnalysisMode;
  result: { matches: SimilarityMatch[]; notice: string; coverage: string; query_duration: number;
    reviewed_tracks?: number; coverage_status?: "partial" | "available_features";
    unavailable_references?: { track_id: string; reason: string }[];
    query_windows?: number; reviewed_windows?: number;
  } | null;
};

export type Corpus = { tracks: number; source: string; available: boolean; snapshot_id: string; algorithms: Record<AnalysisMode, string> };

export const isActive = (status: Production["status"]) =>
  ["queued", "planning", "generating", "saving"].includes(status);

export const statusLabel: Record<Production["status"], string> = {
  queued: "작업 대기", planning: "가사·곡 구성 중", generating: "노래 생성 중",
  saving: "음원 저장 중", ready: "재생 가능", failed: "생성 실패", needs_review: "확인 필요",
};

export async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, { ...options, cache: "no-store" });
  if (!response.ok) {
    let message = "서버에 연결하지 못했어요. API 실행 상태를 확인해 주세요.";
    try {
      const body = await response.json();
      message = typeof body.detail === "string" ? body.detail : "입력 내용과 글자 수를 확인해 주세요.";
    } catch { /* Keep the useful connection message for proxy errors. */ }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

export function duration(value: number) {
  return `${Math.floor(value / 60)}:${Math.floor(value % 60).toString().padStart(2, "0")}`;
}
