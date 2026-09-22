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
  external_id?: string | null;
  in_navidrome?: boolean;
  is_public: boolean;
  is_auto_generated: boolean;
  generated_for_date?: string | null;
  track_count: number;
  created_at: string;
};

export type PlaylistTrackDetail = Track & {
  position: number;
  mood?: string[];
  musical_key?: string | null;
  energy?: number | null;
  similarity?: number | null;
};

export type MediaUser = {
  id: string;
  external_id: string;
  username: string;
  is_admin: boolean;
  last_seen_at?: string | null;
};

export type ArtistEntry = {
  name: string;
  track_count: number;
  top_genres: string[];
  cover_track_id?: string | null;
  cover_art_id?: string | null;
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
  artists: (params: { q?: string; genre?: string[]; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    if (params.q) qs.set("q", params.q);
    (params.genre ?? []).forEach((g) => qs.append("genre", g));
    if (params.limit) qs.set("limit", String(params.limit));
    if (params.offset) qs.set("offset", String(params.offset));
    const s = qs.toString();
    return http<{ items: ArtistEntry[]; total: number; limit: number; offset: number }>(
      `/api/library/artists${s ? `?${s}` : ""}`,
    );
  },
  similarArtists: (name: string, limit = 6) =>
    http<{ items: ArtistEntry[]; server_used: boolean }>(`/api/library/artists/similar?name=${encodeURIComponent(name)}&limit=${limit}`),
  seedTaste: (userId: string, body: { genres: string[]; artists: string[]; track_ids?: string[] }) =>
    http<{ ok: boolean; favorites_added: number; history_added: number; favorites_total: number; error?: string }>(
      `/api/users/${userId}/seed-taste`,
      { method: "POST", body: JSON.stringify(body) },
    ),

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
  trackCoverUrl: (id: string, size: number = 300) => `/api/covers/track/${id}?size=${size}`,
  coverUrl: (coverId: string, size: number = 300) => `/api/covers/${encodeURIComponent(coverId)}?size=${size}`,
  prefetchCovers: (artist_ids?: string[]) => http<{ fetched: number }>(`/api/covers/prefetch`, { method: "POST", body: JSON.stringify({ artist_ids: artist_ids ?? [] }) }),
  // yandex + ai + clap
  getYandexConfig: () => http<{ token: string; enabled: boolean; has_token: boolean }>(`/api/yandex/config`),
  saveYandexConfig: (p: Record<string, unknown>) => http<unknown>(`/api/yandex/config`, { method: "POST", body: JSON.stringify(p) }),
  testYandex: (p?: Record<string, unknown>) => http<{ ok: boolean; result?: unknown; error?: string }>(`/api/yandex/test`, { method: "POST", body: JSON.stringify(p ?? {}) }),
  aiPull: (p?: Record<string, unknown>) => http<{ ok: boolean; error?: string; model?: string }>(`/api/settings/ai/pull`, { method: "POST", body: JSON.stringify(p ?? {}) }),
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

  generateDailyPlaylist: (n = 30, user_id?: string) =>
    http<{ queued: boolean; playlist_id: string; tracks: number; steps: { step: number; name: string; items: number }[] }>(
      `/api/playlists/generate-daily`,
      { method: "POST", body: JSON.stringify(user_id ? { n, user_id } : { n }) },
    ),
  aiGenerate: (query: string, n = 30, user_id?: string) =>
    http<{ playlist_id: string; name: string; comment?: string; tracks: number; from_fallback?: boolean }>(
      `/api/playlists/ai-generate`,
      { method: "POST", body: JSON.stringify({ query, n, user_id }) },
    ),
  listPlaylists: () => http<{ playlists: Playlist[] }>(`/api/playlists/`),
  getPlaylist: (id: string) => http<Playlist & { tracks: PlaylistTrackDetail[] }>(`/api/playlists/${id}`),
  deletePlaylist: (id: string) => http<{ ok: boolean }>(`/api/playlists/${id}`, { method: "DELETE" }),
  exportPlaylist: (id: string) =>
    http<{ ok: boolean; navidrome_id?: string; exported?: number; skipped?: number; error?: string }>(
      `/api/playlists/${id}/export`,
      { method: "POST" },
    ),

  listUsers: () => http<{ users: MediaUser[] }>(`/api/users/`),
  createUser: (body: { external_id: string; username: string; is_admin?: boolean }) =>
    http<{ user: MediaUser }>(`/api/users`, { method: "POST", body: JSON.stringify(body) }),
  createUserByCredentials: (body: { username: string; password: string; remember?: boolean }) =>
    http<{ ok: boolean; user?: MediaUser; import?: Record<string, unknown>; vault_stored?: boolean | string; error?: string }>(
      `/api/users/by-credentials`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  importTastes: (id: string, password: string) =>
    http<{ ok: boolean; favorites_total?: number; favorites_added?: number; playlists?: number; error?: string }>(
      `/api/users/${id}/import-tastes`,
      { method: "POST", body: JSON.stringify({ password }) },
    ),
  userTastes: (id: string) =>
    http<{ user_id: string; favorites: number; playlists: number; playlist_tracks: number }>(
      `/api/users/${id}/tastes`,
    ),
  userProfile: (id: string) =>
    http<{
      ok: boolean; username: string;
      genres: { name: string; weight: number; likes: number; plays: number }[];
      artists: { name: string; weight: number; likes: number; plays: number; dislikes: number; banned: boolean }[];
      moods: { name: string; count: number }[];
      hours: number[]; days: number[];
      top_tracks: { track_id: string; title: string; artist_name?: string; score: number; like: boolean | null; plays: number; skips: number; replays: number; last_played?: string | null }[];
      mobile?: { synced_at?: string | null; counts?: Record<string, number>; genres_top?: [string, number][]; artists_top?: [string, number][] };
      likedSongs: string[]; dislikedSongs: string[]; bannedArtists: string[];
      artistDislikeCounts: Record<string, number>;
      counts: { likes: number; dislikes: number; banned: number; events: number; plays: number };
    }>(`/api/users/${id}/profile`),
  rateTrack: (userId: string, track_id: string, like: boolean | null) =>
    http<{ ok: boolean; auto_banned_artist?: string | null }>(
      `/api/users/${userId}/rate`,
      { method: "POST", body: JSON.stringify({ track_id, like }) },
    ),
  pushEvents: (userId: string, events: { track_id: string; action: string; position_sec?: number }[]) =>
    http<{ ok: boolean; stored: number; auto_bans: string[] }>(
      `/api/users/${userId}/events`,
      { method: "POST", body: JSON.stringify({ events }) },
    ),
  banArtist: (userId: string, artist_name: string) =>
    http<{ ok: boolean }>(`/api/users/${userId}/ban-artist`, { method: "POST", body: JSON.stringify({ artist_name }) }),
  unbanArtist: (userId: string, artist_name: string) =>
    http<{ ok: boolean }>(`/api/users/${userId}/unban-artist`, { method: "POST", body: JSON.stringify({ artist_name }) }),
  vaultStatus: (id: string) => http<{ stored: boolean; available: boolean }>(`/api/users/${id}/vault`),
  vaultStore: (id: string, password: string) =>
    http<{ ok: boolean; error?: string }>(`/api/users/${id}/vault`, { method: "POST", body: JSON.stringify({ password }) }),
  vaultForget: (id: string) => http<{ ok: boolean }>(`/api/users/${id}/vault`, { method: "DELETE" }),
  refreshNow: (id: string) =>
    http<{ ok: boolean; favorites_total?: number; playlists?: number; error?: string }>(
      `/api/users/${id}/refresh-now`, { method: "POST" },
    ),
  myWave: (user_id: string, n = 30, seed_track_id?: string, mood?: string) =>
    http<{ playlist_id: string; tracks: number; excluded_disliked: number; excluded_banned: number }>(
      `/api/playlists/my-wave`,
      { method: "POST", body: JSON.stringify({ user_id, n, seed_track_id, mood }) },
    ),
  collabSimilar: (userId: string) =>
    http<{ users: { user_id: string; username: string; similarity: number; shared_likes: number; likes: number }[] }>(
      `/api/collab/similar-users/${userId}`,
    ),
  collabRecommend: (userId: string, n = 30) =>
    http<{ items: { track_id: string; title: string; artist_name?: string; score: number; because_of: string[] }[] }>(
      `/api/collab/recommend/${userId}?n=${n}`,
    ),
  collabCompare: (user_ids: string[], top_n = 12) =>
    http<{
      ok: boolean;
      users: { user_id: string; username: string; likes: number; genres_top: { name: string; weight: number }[]; artists_top: { name: string; weight: number }[] }[];
      shared_genres: { name: string; weights: Record<string, number>; avg: number }[];
      shared_artists: { name: string; weights: Record<string, number>; avg: number }[];
      shared_tracks: { track_id: string; title: string; artist_name?: string; liked_by: string[] }[];
      pairwise: { a: string; b: string; a_name: string; b_name: string; similarity: number; shared_likes: number }[];
    }>(`/api/collab/compare`, { method: "POST", body: JSON.stringify({ user_ids, top_n }) }),
  duplicates: () =>
    http<{ groups: { key: string; keep_id: string; tracks: { id: string; title: string; artist_name?: string; album_name?: string; duration_sec?: number; play_count: number; starred: boolean; source: string }[] }[]; group_count: number; duplicate_tracks: number }>(
      `/api/library/duplicates`,
    ),
  mergeDuplicates: (keep_id: string, drop_ids: string[]) =>
    http<{ ok: boolean; merged?: number; error?: string }>(
      `/api/library/duplicates/merge`,
      { method: "POST", body: JSON.stringify({ keep_id, drop_ids }) },
    ),
  autoMergeDuplicates: () =>
    http<{ ok: boolean; groups: number; merged: number }>(`/api/library/duplicates/auto`, { method: "POST" }),
  getDedupSettings: () => http<{ auto_merge: boolean }>(`/api/library/dedup-settings`),
  saveDedupSettings: (auto_merge: boolean) =>
    http<{ ok: boolean }>(`/api/library/dedup-settings`, { method: "POST", body: JSON.stringify({ auto_merge }) }),
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
