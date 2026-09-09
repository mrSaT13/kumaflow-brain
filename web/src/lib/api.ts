export type Track = {
  id: string;
  title: string;
  artist_name?: string;
  album_name?: string;
  genre?: string;
  duration_sec?: number;
  year?: number;
  cover_art_id?: string;
  play_count?: number;
  rating?: number;
  starred?: boolean;
  last_played_at?: string | null;
  server_id?: string | null;
  source?: "navidrome" | "disk" | "demo" | string;
};

export type TrackDetail = Track & {
  features?: Record<string, number | string | null>;
  moods?: string[];
  mood_vector?: Record<string, unknown>;
  lyrics?: { provider: string; text: string; language?: string; source_url?: string } | null;
  metadata?: Record<string, Record<string, unknown>>;
  cluster?: { id: number; algorithm: string } | null;
};

export type ScanRun = {
  id: string;
  phase: string;
  status: "queued" | "running" | "success" | "failure";
  total_items: number;
  processed_items: number;
  started_at: string;
  finished_at?: string | null;
  error?: string | null;
  job_id?: string | null;
  cancellable?: boolean;
};

export type LogLine = {
  id: string;
  level: "info" | "warn" | "error" | string;
  message: string;
  created_at: string;
};

export type Playlist = {
  id: string;
  name: string;
  is_public: boolean;
  is_auto_generated: boolean;
  generated_for_date?: string | null;
  track_count: number;
  created_at: string;
};

export type MediaUser = {
  id: string;
  external_id: string;
  username: string;
  is_admin: boolean;
  last_seen_at?: string | null;
};

const BASE = process.env.NEXT_PUBLIC_KUMAFLOW_API ?? "";

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => http<{ status: string }>("/api/health"),
  version: () => http<{ name: string; version: string }>("/api/version"),

  overview: () =>
    http<{
      tracks: number;
      albums: number;
      artists: number;
      users: number;
      servers: number;
      active_server?: string | null;
      disk_tracks?: number;
      navidrome_tracks?: number;
    }>("/api/library/overview"),
  servers: () =>
    http<{ servers: { id: string; type: string; name: string; url: string; enabled: boolean }[] }>(
      "/api/library/servers",
    ),
  genres: () => http<{ genres: string[] }>("/api/library/genres"),

  listTracks: (params: Record<string, string | number | undefined>) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== undefined) as [string, string][],
    ).toString();
    return http<{ items: Track[]; total: number; limit: number; offset: number }>(
      `/api/tracks/${qs ? `?${qs}` : ""}`,
    );
  },
  getTrack: (id: string) => http<TrackDetail>(`/api/tracks/${id}`),
  analyzeTrack: (id: string) =>
    http<{ queued: boolean; run_id: string; job_id: string }>(`/api/tracks/${id}/analyze`, { method: "POST" }),
  fetchTrackLyrics: (id: string) =>
    http<{ ok: boolean; error?: string }>(`/api/tracks/${id}/lyrics`, { method: "POST" }),
  trackCoverUrl: (id: string) => `/api/covers/track/${id}`,
  coverUrl: (coverId: string) => `/api/covers/${encodeURIComponent(coverId)}`,
  recommendByTrack: (id: string) => http<{ items: Track[] }>(`/api/analysis/recommend/by-track/${id}`),
  coldStart: (n = 30) => http<{ tracks: string[]; items: Track[]; steps: { step: number; name: string; items: number }[] }>(`/api/analysis/cold-start?n=${n}`),

  startLibraryScan: () => http<{ queued: boolean; run_id: string }>(`/api/scan/library`, { method: "POST" }),
  startAnalysis: () => http<{ queued: boolean; run_id: string }>(`/api/scan/analysis`, { method: "POST" }),
  startLyrics: () => http<{ queued: boolean; run_id: string }>(`/api/scan/lyrics`, { method: "POST" }),
  startClusters: () => http<{ queued: boolean; run_id: string }>(`/api/scan/clusters`, { method: "POST" }),
  startCollab: () => http<{ queued: boolean; run_id: string }>(`/api/scan/collab`, { method: "POST" }),
  listRuns: () => http<{ runs: ScanRun[] }>(`/api/scan/runs`),
  currentRun: () => http<{ current: ScanRun | null }>(`/api/scan/runs/current`),
  runLogs: (id: string) => http<{ logs: LogLine[] }>(`/api/scan/runs/${id}/logs`),
  cancelRun: (id: string) =>
    http<{ ok: boolean; error?: string }>(`/api/scan/runs/${id}/cancel`, { method: "POST" }),

  generateDailyPlaylist: (n = 30) =>
    http<{ queued: boolean; playlist_id: string; tracks: number; steps: { step: number; name: string; items: number }[] }>(
      `/api/playlists/generate-daily`,
      { method: "POST", body: JSON.stringify({ n }) },
    ),
  listPlaylists: () => http<{ playlists: Playlist[] }>(`/api/playlists/`),
  getPlaylist: (id: string) => http<Playlist & { tracks: Track[] }>(`/api/playlists/${id}`),
  deletePlaylist: (id: string) => http<{ ok: boolean }>(`/api/playlists/${id}`, { method: "DELETE" }),

  listUsers: () => http<{ users: MediaUser[] }>(`/api/users/`),
  createUser: (body: { external_id: string; username: string; is_admin?: boolean }) =>
    http<{ user: MediaUser }>(`/api/users`, { method: "POST", body: JSON.stringify(body) }),
  updateUser: (id: string, body: { username?: string; is_admin?: boolean }) =>
    http<{ user: MediaUser }>(`/api/users/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteUser: (id: string) => http<{ ok: boolean }>(`/api/users/${id}`, { method: "DELETE" }),
  syncUsers: () =>
    http<{ ok: boolean; added?: number; updated?: number; total?: number; error?: string }>(
      `/api/users/sync`,
      { method: "POST" },
    ),

  buildClusters: () =>
    http<{ queued: boolean; run_id: string; job_id: string }>(`/api/clusters/build`, { method: "POST" }),
  listClusters: () =>
    http<{
      clusters: {
        id: number;
        algorithm: string;
        size: number;
        top_genres: string[];
        avg_energy: number | null;
        sample: { id: string; title: string; artist_name: string }[];
      }[];
    }>(`/api/clusters/`),

  fetchLyrics: () => http<{ queued: boolean }>(`/api/playlists/fetch-lyrics`, { method: "POST" }),

  settings: () =>
    http<{ runtime: Record<string, unknown>; db: Record<string, unknown> }>(`/api/settings/`),  saveMediaServer: (body: Record<string, string>) =>
    http<{ ok: boolean; saved?: Record<string, string>; error?: string }>(`/api/settings/media-server`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getMediaServer: () => http<Record<string, string>>(`/api/settings/media-server`),
  testMediaServer: (body?: Record<string, string>) =>
    http<{ ok: boolean; error?: string; status?: number; body?: string }>(`/api/settings/media-server/test`, {
      method: "POST",
      body: JSON.stringify(body ?? {}),
    }),
  aiTest: (prompt: string) =>
    http<{ ok: boolean; response?: string; error?: string }>(`/api/settings/ai/test`, {
      method: "POST",
      body: JSON.stringify({ prompt }),
    }),
  aiModels: () =>
    http<{ provider: string; configured_model: string; available: string[] }>(`/api/settings/ai/models`),
  getAi: () =>
    http<{
      saved: Record<string, string>;
      effective: Record<string, string>;
      configured: boolean;
    }>(`/api/settings/ai`),
  saveAi: (body: Record<string, string>) =>
    http<{ ok: boolean; error?: string }>(`/api/settings/ai`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  getBridge: () => http<{ url: string; enabled: boolean }>(`/api/settings/bridge`),
  saveBridge: (body: { url: string; enabled: boolean }) =>
    http<{ ok: boolean; saved?: { url: string; enabled: boolean }; error?: string }>(
      `/api/settings/bridge`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  testBridge: (body?: { url: string }) =>
    http<{ ok: boolean; error?: string; status?: number; body?: unknown }>(`/api/settings/bridge/test`, {
      method: "POST",
      body: JSON.stringify(body ?? {}),
    }),
  bridgeStatus: () =>
    http<{ enabled: boolean; url: string; reachable: boolean; error?: string }>(`/api/bridge/status`),
};
