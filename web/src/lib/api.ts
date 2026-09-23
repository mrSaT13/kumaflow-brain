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
  is_hidden?: boolean;
  generated_for_date?: string | null;
  track_count: number;
  created_at: string;
  owner_user_id?: string | null;
  owner_username?: string | null;
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

const TOKEN_KEY = "kumaflow_api_token";

export function getBrowserToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setBrowserToken(v: string) {
  try {
    if (v) localStorage.setItem(TOKEN_KEY, v);
    else localStorage.removeItem(TOKEN_KEY);
  } catch { /* приватный режим */ }
}

function authHeaders(): Record<string, string> {
  const t = getBrowserToken().trim();
  return t ? { authorization: `Bearer ${t}` } : {};
}

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...authHeaders(), ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    // backend обычно отдаёт JSON {ok:false,error} или {detail} — показываем его, а не HTML
    try {
      const j = JSON.parse(text);
      const msg = (j as { error?: string; detail?: string }).error ?? (j as { detail?: string }).detail ?? text;
      throw new Error(`${res.status}: ${String(msg).slice(0, 400)}`);
    } catch (e) {
      if (e instanceof Error && /^\d+:/.test(e.message)) throw e;
      throw new Error(`${res.status}: ${text.slice(0, 200)}`);
    }
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
      analyzed_tracks?: number;
    }>("/api/library/overview"),
  servers: () =>
    http<{ servers: { id: string; type: string; name: string; url: string; enabled: boolean }[] }>(
      "/api/library/servers",
    ),
  genres: () => http<{ genres: string[] }>("/api/library/genres"),
  libraryHealth: () =>
    http<{
      ok: boolean; total: number; low_bitrate: number; no_cover: number;
      no_lyrics: number; not_analyzed: number; no_genre: number;
      duplicate_groups: number; duplicate_tracks: number;
      samples: { low_bitrate: { track_id: string; title: string; artist_name?: string }[]; no_lyrics: { track_id: string; title: string; artist_name?: string }[] };
    }>(`/api/library/health`),
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
  trackCoverUrl: (id: string, size: number = 300) => {
    const t = getBrowserToken().trim();
    return `/api/covers/track/${id}?size=${size}${t ? `&token=${encodeURIComponent(t)}` : ""}`;
  },
  coverUrl: (coverId: string, size: number = 300) => {
    const t = getBrowserToken().trim();
    return `/api/covers/${encodeURIComponent(coverId)}?size=${size}${t ? `&token=${encodeURIComponent(t)}` : ""}`;
  },
  prefetchCovers: (artist_ids?: string[]) => http<{ fetched: number }>(`/api/covers/prefetch`, { method: "POST", body: JSON.stringify({ artist_ids: artist_ids ?? [] }) }),
  // yandex + ai + clap
  getYandexConfig: () => http<{ token: string; enabled: boolean; has_token: boolean }>(`/api/yandex/config`),
  saveYandexConfig: (p: Record<string, unknown>) => http<unknown>(`/api/yandex/config`, { method: "POST", body: JSON.stringify(p) }),
  testYandex: (p?: Record<string, unknown>) => http<{ ok: boolean; result?: unknown; error?: string }>(`/api/yandex/test`, { method: "POST", body: JSON.stringify(p ?? {}) }),
  yandexUserTokenSave: (user_id: string, token: string) =>
    http<{ ok: boolean; login?: string | null; error?: string }>(`/api/yandex/user-token`, { method: "POST", body: JSON.stringify({ user_id, token }) }),
  yandexUserTokenStatus: (user_id: string) =>
    http<{ ok: boolean; stored: boolean }>(`/api/yandex/user-token-status?user_id=${encodeURIComponent(user_id)}`),
  yandexUserTokenForget: (user_id: string) =>
    http<{ ok: boolean }>(`/api/yandex/user-token?user_id=${encodeURIComponent(user_id)}`, { method: "DELETE" }),
  yandexImportSettings: () =>
    http<{ ok: boolean; settings: { history_enabled: boolean; charts_enabled: boolean } }>(`/api/yandex/import-settings`),
  saveYandexImportSettings: (body: { history_enabled?: boolean; charts_enabled?: boolean; corrections_enabled?: boolean }) =>
    http<{ ok: boolean; settings: { history_enabled: boolean; charts_enabled: boolean } }>(`/api/yandex/import-settings`, { method: "PUT", body: JSON.stringify(body) }),
  yandexImportTaste: (user_id: string) =>
    http<{ ok: boolean; error?: string; [k: string]: unknown }>(`/api/yandex/import-taste`, { method: "POST", body: JSON.stringify({ user_id }) }),
  yandexImportHistory: (user_id: string, limit = 300) =>
    http<{ ok: boolean; error?: string; [k: string]: unknown }>(`/api/yandex/import-history`, { method: "POST", body: JSON.stringify({ user_id, limit }) }),
  yandexChartsRefresh: () =>
    http<{ ok: boolean; error?: string; tracks?: number; fetched_at?: string }>(`/api/yandex/charts/refresh`, { method: "POST" }),
  yandexCharts: () =>
    http<{ ok: boolean; fetched_at?: string | null; total: number; in_library: number; tracks: { artist?: string; title?: string; origin?: string; track_id?: string }[] }>(`/api/yandex/charts`),
  yandexCorrections: (limit = 50, offset = 0) =>
    http<{ ok: boolean; total: number; items: { track_id: string; title: string; artist_name?: string; album_name?: string | null; corrected: Record<string, [unknown, unknown]>; fetched_at?: string | null }[] }>(
      `/api/yandex/corrections?limit=${limit}&offset=${offset}`),
  yandexRevertCorrection: (track_id: string, fields?: string[]) =>
    http<{ ok: boolean; error?: string; restored?: Record<string, [unknown, unknown]> }>(
      `/api/yandex/corrections/revert`, { method: "POST", body: JSON.stringify({ track_id, fields }) }),
  yandexTrackMeta: (track_id: string) =>
    http<{ items: { source: string; data: Record<string, unknown>; fetched_at?: string | null }[] }>(`/api/yandex/track/${track_id}`),
  aiPull: (p?: Record<string, unknown>) => http<{ ok: boolean; error?: string; model?: string }>(`/api/settings/ai/pull`, { method: "POST", body: JSON.stringify(p ?? {}) }),
  recommendByTrack: (id: string) => http<{ items: Track[] }>(`/api/analysis/recommend/by-track/${id}`),
  coldStart: (n = 30) => http<{ tracks: string[]; items: Track[]; steps: { step: number; name: string; items: number }[] }>(`/api/analysis/cold-start?n=${n}`),

  startLibraryScan: () => http<{ queued: boolean; run_id: string }>(`/api/scan/library`, { method: "POST" }),
  startAnalysis: (limit = 0, force = false) => http<{ queued: boolean; run_id: string }>(`/api/scan/analysis${limit || force ? `?limit=${limit}${force ? "&force=true" : ""}` : ""}`, { method: "POST" }),
  startLyrics: () => http<{ queued: boolean; run_id: string }>(`/api/scan/lyrics`, { method: "POST" }),
  startClusters: () => http<{ queued: boolean; run_id: string }>(`/api/scan/clusters`, { method: "POST" }),
  startCollab: () => http<{ queued: boolean; run_id: string }>(`/api/scan/collab`, { method: "POST" }),
  startSmart: () => http<{ queued: boolean; run_id: string }>(`/api/scan/smart`, { method: "POST" }),
  purgeRuns: (keep_last = 20) => http<{ ok: boolean; deleted_runs: number; deleted_logs: number }>(`/api/scan/runs?keep_last=${keep_last}`, { method: "DELETE" }),
  clearAllHistory: () => http<{ ok: boolean; cleared_history: number; cleared_events: number }>(`/api/users/history`, { method: "DELETE" }),
  clearUserHistory: (id: string) => http<{ ok: boolean }>(`/api/users/${id}/history`, { method: "DELETE" }),
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
  weeklyDiscovery: (user_id: string, n = 30) =>
    http<{ playlist_id: string; tracks: number; mode: string; explanations: { track_id: string; score: number; because_of_title?: string | null; genre_match?: boolean; mood?: string | null; text: string }[]; steps: { step: number; name: string; items: number }[] }>(
      `/api/playlists/weekly-discovery`,
      { method: "POST", body: JSON.stringify({ user_id, n }) },
    ),
  aiGenerate: (query: string, n = 30, user_id?: string) =>
    http<{ playlist_id: string; name: string; comment?: string; tracks: number; from_fallback?: boolean }>(
      `/api/playlists/ai-generate`,
      { method: "POST", body: JSON.stringify({ query, n, user_id }) },
    ),
  listPlaylists: (show_hidden = false) => http<{ playlists: Playlist[]; hidden_count?: number }>(`/api/playlists/${show_hidden ? "?show_hidden=true" : ""}`),
  getPlaylist: (id: string) => http<Playlist & { tracks: PlaylistTrackDetail[] }>(`/api/playlists/${id}`),
  deletePlaylist: (id: string) => http<{ ok: boolean }>(`/api/playlists/${id}`, { method: "DELETE" }),
  hidePlaylist: (id: string) => http<{ ok: boolean }>(`/api/playlists/${id}/hide`, { method: "POST" }),
  unhidePlaylist: (id: string) => http<{ ok: boolean }>(`/api/playlists/${id}/unhide`, { method: "POST" }),
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
  importTastes: (id: string, password: string, include_playlists = false) =>
    http<{ ok: boolean; favorites_total?: number; favorites_added?: number; playlists?: number; error?: string }>(
      `/api/users/${id}/import-tastes`,
      { method: "POST", body: JSON.stringify({ password, include_playlists }) },
    ),
  syncPlaylists: (id: string, password: string) =>
    http<{ ok: boolean; playlists?: number; playlist_tracks?: number; error?: string }>(
      `/api/users/${id}/sync-playlists`,
      { method: "POST", body: JSON.stringify({ password }) },
    ),
  nowPlaying: (userId?: string, n = 5, offset = 0, seed?: string) => {
    const qs = new URLSearchParams();
    if (userId) qs.set("user_id", userId);
    qs.set("n", String(n));
    if (offset) qs.set("offset", String(offset));
    if (seed) qs.set("seed", seed);
    return http<{
      playing: {
        external_id?: string | null; track_id?: string | null; title: string;
        artist_name: string; album_name?: string | null; username?: string | null;
        minutes_ago?: number | null; player?: string | null; cover_art_id?: string | null;
      } | null;
      next: {
        track_id: string; title: string; artist_name?: string; album_name?: string | null;
        genre?: string | null; score: number; reason: string; cover_art_id?: string | null;
      }[];
      source: string;
      offset?: number;
    }>(`/api/now-playing/?${qs.toString()}`);
  },
  userTastes: (id: string) =>
    http<{ user_id: string; favorites: number; playlists: number; playlist_tracks: number }>(
      `/api/users/${id}/tastes`,
    ),
  wrapped: (id: string, year?: number, month?: number) =>
    http<{
      ok: boolean; month: string; months: string[]; user_id: string;
      plays: number; minutes: number; active_days: number; peak_hour: number; hours: number[];
      top_tracks: { track_id: string; title: string; artist_name?: string; plays: number }[];
      top_artists: { name: string; plays: number }[];
      top_genres: { name: string; plays: number }[];
      discoveries: number; discoveries_sample: { track_id: string; title: string; artist_name?: string }[];
      new_likes: number; new_dislikes: number; new_bans: number;
      skips: number; replays: number; completes: number;
    }>(`/api/users/${id}/wrapped${year && month ? `?year=${year}&month=${month}` : ""}`),
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
  drift: (id: string, weeks_ago = 4) =>
    http<{
      ok: boolean; summary?: string | null; snapshot_week?: string; weeks: string[];
      genres_up: { name: string; old: number; new: number; delta: number }[];
      genres_down: { name: string; old: number; new: number; delta: number }[];
      artists_up: { name: string; old: number; new: number; delta: number }[];
      artists_down: { name: string; old: number; new: number; delta: number }[];
      hint?: string;
    }>(`/api/users/${id}/drift?weeks_ago=${weeks_ago}`),
  takeDriftSnapshot: (id: string) =>
    http<{ ok: boolean; week?: string }>(`/api/users/${id}/drift/snapshot`, { method: "POST" }),
  userActivity: (id: string, year?: number) =>
    http<{ ok: boolean; user_id: string; year: number; days: Record<string, number>; total: number; active_days: number }>(
      `/api/users/${id}/activity${year ? `?year=${year}` : ""}`,
    ),
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
  vaultStatus: (id: string) => http<{ stored: boolean; available: boolean; key_source?: string }>(`/api/users/${id}/vault`),
  vaultStore: (id: string, password: string) =>
    http<{ ok: boolean; stored?: boolean; key_created?: boolean; error?: string }>(`/api/users/${id}/vault`, { method: "POST", body: JSON.stringify({ password }) }),
  vaultForget: (id: string) => http<{ ok: boolean }>(`/api/users/${id}/vault`, { method: "DELETE" }),
  refreshNow: (id: string) =>
    http<{ ok: boolean; favorites_total?: number; playlists?: number; error?: string }>(
      `/api/users/${id}/refresh-now`, { method: "POST" },
    ),
  refreshAsync: (id: string) =>
    http<{ ok: boolean; queued?: boolean; run_id?: string; job_id?: string; error?: string }>(
      `/api/users/${id}/refresh-async`, { method: "POST" },
    ),
  myWave: (user_id: string, n = 30, seed_track_id?: string, mood?: string) =>
    http<{ playlist_id: string; tracks: number; excluded_disliked: number; excluded_banned: number }>(
      `/api/playlists/my-wave`,
      { method: "POST", body: JSON.stringify({ user_id, n, seed_track_id, mood }) },
    ),
  waveContinue: (body: {
    user_id: string; queue?: string[]; current_track_id?: string; count?: number;
    settings?: Record<string, string>; exclude_ids?: string[];
    recent_events?: { track_id: string; action: string; position_sec?: number }[];
    ratings_delta?: Record<string, unknown>[];
  }) =>
    http<{
      ok: boolean; user_id: string;
      tracks: { track_id: string; title: string; artist_name?: string; score: number; reason: string; cover_art_id?: string | null; mood?: string | null }[];
      seeds: string[]; applied: Record<string, number>;
      drift?: { severity: string; consecutive_skips: number; temp_banned_genres: string[] } | null;
    }>(`/api/wave/continue`, { method: "POST", body: JSON.stringify(body) }),
  waveSeeds: (user_id: string, limit = 5) =>
    http<{ ok: boolean; user_id: string; seeds: string[] }>(
      `/api/wave/seeds?user_id=${encodeURIComponent(user_id)}&limit=${limit}`,
    ),
  wavePublish: (body: { user_id: string; queue: string[]; current_track_id?: string }) =>
    http<{ ok: boolean; queued: number }>(`/api/wave/publish`, { method: "POST", body: JSON.stringify(body) }),
  waveLive: (user_id: string) =>
    http<{
      ok: boolean; user_id: string; current: number; age_sec?: number | null; stale?: boolean;
      queue: { track_id: string; title: string; artist_name?: string; album_name?: string | null; genre?: string | null; cover_art_id?: string | null; reason: string }[];
    }>(`/api/wave/live?user_id=${encodeURIComponent(user_id)}`),
  listCron: () =>
    http<{ jobs: { id: string; name: string; kind: string; cron_expr: string; enabled: boolean; last_run_at?: string | null }[] }>(
      `/api/cron/`,
    ),
  updateCron: (id: string, body: { enabled?: boolean; cron_expr?: string; name?: string }) =>
    http<{ ok: boolean }>(`/api/cron/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  runCron: (id: string) =>
    http<{ queued: boolean; job_id?: string; run_id?: string; error?: string }>(
      `/api/cron/${id}/run`, { method: "POST" },
    ),
  clapStatus: () =>
    http<{ available: boolean; files: { name: string; bytes: number }[]; embeddings: Record<string, number>; audio_stub: boolean }>(
      `/api/analysis/clap-status`,
    ),
  getAutomation: () =>
    http<{ ok: boolean; flags: { analysis_fetch_lyrics?: boolean; analysis_ai_mood?: boolean; playlists_push_navidrome?: boolean } }>(`/api/settings/automation`),
  saveAutomation: (flags: { analysis_fetch_lyrics?: boolean; analysis_ai_mood?: boolean; playlists_push_navidrome?: boolean }) =>
    http<{ ok: boolean; flags: Record<string, unknown> }>(
      `/api/settings/automation`, { method: "PUT", body: JSON.stringify(flags) },
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
  fingerprintDuplicates: (threshold = 0.985) =>
    http<{ groups: { type: string; score: number; keep_id: string; drop_ids: string[]; tracks: { id: string; title: string; artist_name?: string; duration_sec?: number }[] }[]; group_count: number; duplicate_tracks: number; threshold: number }>(
      `/api/library/duplicates/fingerprint?threshold=${threshold}`,
    ),
  autoMergeFingerprint: (threshold = 0.985, dry_run = false) =>
    http<{ ok: boolean; groups: number; merged?: number; tracks?: number }>(
      `/api/library/duplicates/auto-fingerprint`,
      { method: "POST", body: JSON.stringify({ threshold, dry_run }) },
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

  notifications: (limit = 50, all = true) =>
    http<{ notifications: { id: string; user_id?: string | null; kind: string; title: string; body?: string | null; link?: string | null; created_at?: string | null; read_at?: string | null }[] }>(
      `/api/notifications/?limit=${limit}${all ? "&all=true" : ""}`,
    ),
  unreadCount: (all = true) => http<{ unread: number }>(`/api/notifications/unread-count${all ? "?all=true" : ""}`),
  markNotificationRead: (id: string) =>
    http<{ ok: boolean }>(`/api/notifications/${id}/read`, { method: "POST" }),
  markAllNotificationsRead: (all = true) =>
    http<{ ok: boolean; marked: number }>(`/api/notifications/read-all${all ? "?all=true" : ""}`, { method: "POST" }),

  tokensMeta: () =>
    http<{
      scopes: Record<string, string>;
      presets: Record<string, { label: string; desc: string; scopes: string[] }>;
      env_token_configured: boolean; count: number;
    }>(`/api/settings/tokens/meta`),  listTokens: (owner_user_id?: string) =>
    http<{ tokens: ApiToken[] }>(`/api/settings/tokens${owner_user_id ? `?owner_user_id=${encodeURIComponent(owner_user_id)}` : ""}`),
  createToken: (body: { owner_user_id?: string | null; name: string; scopes: string[] }) =>
    http<{ ok: boolean; id: string; name: string; prefix: string; scopes: string[]; owner_user_id?: string | null; token: string }>(
      `/api/settings/tokens`, { method: "POST", body: JSON.stringify(body) },
    ),
  toggleToken: (id: string, enabled: boolean) =>
    http<{ ok: boolean; token: ApiToken }>(`/api/settings/tokens/${id}`, { method: "PATCH", body: JSON.stringify({ enabled }) }),
  deleteToken: (id: string) =>
    http<{ ok: boolean }>(`/api/settings/tokens/${id}`, { method: "DELETE" }),

  login: (body: { username: string; password: string; device?: string }) =>
    http<{
      ok: boolean; token?: string; user?: { id: string; username: string };
      is_admin?: boolean; scopes?: string[]; error?: string;
    }>(`/api/settings/login`, { method: "POST", body: JSON.stringify(body) }),
  whoami: () =>
    http<{
      ok: boolean; logged_in: boolean; is_admin: boolean;
      owner_user_id?: string | null; prefix?: string;
      locked: boolean; tokens_exist: boolean; env_configured: boolean;
    }>(`/api/settings/whoami`),
};

export type ApiToken = {
  id: string; name: string; prefix: string; scopes: string[];
  owner_user_id?: string | null; enabled: boolean;
  created_at?: string | null; last_used_at?: string | null;
};
