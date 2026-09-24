"use client";

import Link from "next/link";
import useSWR from "swr";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowRight, Loader2, Play } from "lucide-react";
import { Button, Card, PageHeader, Section } from "@/components/ui";
import { useToast, fmtErr } from "@/components/toasts";
import { api } from "@/lib/api";
import TrackCover from "@/components/TrackCover";
import { moodLook } from "@/lib/moodStyle";

type WaveComp = {
  audio?: number; genre?: number; artist?: number;
  behavior?: number; collab?: number; novelty?: number;
};

type WaveTrack = {
  track_id: string; title: string; artist_name?: string; score: number; reason: string;
  mood?: string | null; moods?: string[]; energy?: number | null; tempo?: number | null;
  cover_art_id?: string | null; comp?: WaveComp | null;
};

type WaveEvent = { track_id: string; action: string; position_sec?: number };

const REFILL_THRESHOLD = 3;

const COMP_LABELS: { key: keyof WaveComp; label: string }[] = [
  { key: "audio", label: "аудио" },
  { key: "genre", label: "жанр" },
  { key: "artist", label: "артист" },
  { key: "behavior", label: "поведение" },
  { key: "collab", label: "коллаб" },
  { key: "novelty", label: "новизна" },
];

export default function WavePage() {
  const { data: users } = useSWR("/api/users", () => api.listUsers());
  const [userId, setUserId] = useState("");
  const [mood, setMood] = useState("");
  const [queue, setQueue] = useState<WaveTrack[]>([]);
  const [playingIdx, setPlayingIdx] = useState(0);
  const [busy, setBusy] = useState(false);
  const [liveRefill, setLiveRefill] = useState(true);
  const [followNavidrome, setFollowNavidrome] = useState(true);
  const [followPhone, setFollowPhone] = useState(true);
  const [phoneAge, setPhoneAge] = useState<number | null>(null);
  const [drift, setDrift] = useState<{ severity: string; consecutive_skips: number; temp_banned_genres: string[] } | null>(null);
  const toast = useToast();
  const itemRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  const listRef = useRef<HTMLDivElement>(null);
  const [showJump, setShowJump] = useState(false);
  const pendingEvents = useRef<WaveEvent[]>([]);
  const lastSyncedExternal = useRef<string>("");
  const bootRef = useRef<string>("");
  const busyRef = useRef(false);
  busyRef.current = busy;

  const ids = useMemo(() => users?.users ?? [], [users]);
  useEffect(() => {
    if (!userId && ids.length > 0) setUserId(ids[0].id);
  }, [ids, userId]);

  const { data: profile } = useSWR(userId ? ["wave-moods", userId] : null, () => api.userProfile(userId));
  const moodOptions = useMemo(() => (profile?.moods ?? []).map((m) => m.name), [profile]);

  const cur = queue[Math.min(playingIdx, queue.length - 1)] ?? null;
  const nxt = queue[Math.min(playingIdx + 1, queue.length - 1)] ?? null;

  useEffect(() => {
    const el = itemRefs.current.get(playingIdx);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [playingIdx, queue.length]);

  const scrollToCurrent = useCallback(() => {
    const el = itemRefs.current.get(playingIdx);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
    else listRef.current?.scrollTo({ top: 0, behavior: "smooth" });
  }, [playingIdx]);

  // Кнопка «К текущему»: показываем, только если активный трек вне видимости списка.
  useEffect(() => {
    const root = listRef.current;
    if (!root || queue.length === 0) {
      setShowJump(false);
      return;
    }
    const check = () => {
      const el = itemRefs.current.get(playingIdx);
      if (!el) {
        setShowJump(false);
        return;
      }
      const r = el.getBoundingClientRect();
      const c = root.getBoundingClientRect();
      const out = r.top < c.top + 8 || r.bottom > c.bottom - 8;
      setShowJump(out);
    };
    check();
    root.addEventListener("scroll", check, { passive: true });
    window.addEventListener("resize", check);
    const id = setInterval(check, 1000);
    return () => {
      root.removeEventListener("scroll", check);
      window.removeEventListener("resize", check);
      clearInterval(id);
    };
  }, [playingIdx, queue.length]);

  function drainEvents(): WaveEvent[] {
    const out = pendingEvents.current;
    pendingEvents.current = [];
    return out;
  }

  const more = useCallback(async (reset = false) => {
    if (!userId) {
      toast("Выберите пользователя", "err");
      return;
    }
    if (busyRef.current) return;
    setBusy(true);
    try {
      const q = reset ? [] : queue.map((t) => t.track_id);
      const events = drainEvents().slice(-50);
      const r = await api.waveContinue({
        user_id: userId,
        queue: q,
        current_track_id: q.length > 0 ? q[q.length - 1] : undefined,
        count: 10,
        settings: mood ? { mood } : {},
        recent_events: events,
      });
      const tracks = (r.tracks ?? []) as WaveTrack[];
      setQueue((prev) => (reset ? tracks : [...prev, ...tracks]).slice(0, 100));
      setDrift(r.drift ?? null);
      if (reset) {
        setPlayingIdx(0);
        lastSyncedExternal.current = "";
      }
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId, queue, mood, toast]);

  // Холодный вход: очередь пуста, а в Navidrome что-то играет —
  // сразу строим волну от него, жать «Запустить» не надо.
  useEffect(() => {
    if (!followNavidrome || !userId || queue.length > 0 || busyRef.current) return;
    if (bootRef.current === `${userId}:${mood}`) return;
    bootRef.current = `${userId}:${mood}`;
    (async () => {
      try {
        const np = await api.nowPlaying(userId, 1);
        const tid = np.playing?.track_id;
        if (!tid) return;
        setBusy(true);
        const r = await api.waveContinue({
          user_id: userId,
          queue: [],
          current_track_id: tid,
          count: 10,
          settings: mood ? { mood } : {},
          recent_events: [{ track_id: tid, action: "play" }],
        });
        const tracks = (r.tracks ?? []) as WaveTrack[];
        if (tracks.length > 0) {
          setQueue(tracks.slice(0, 100));
          setPlayingIdx(0);
        }
      } catch {
        /* внешний плеер молчит — ждём ручного запуска */
      } finally {
        setBusy(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId, followNavidrome, mood]);

  // Клик по строке = «это сейчас играет во внешнем плеере»: помечаем и копим play-событие.
  function markCurrent(i: number) {
    if (queue.length === 0) return;
    const clamped = Math.max(0, Math.min(i, queue.length - 1));
    const t = queue[clamped];
    if (t && clamped !== playingIdx) {
      pendingEvents.current.push({ track_id: t.track_id, action: "play" });
    }
    setPlayingIdx(clamped);
  }

  // Авто-докрутка хвоста: осталось мало — добрать +10 с накопленными событиями.
  useEffect(() => {
    if (!liveRefill || queue.length === 0 || busy) return;
    if (queue.length - 1 - playingIdx <= REFILL_THRESHOLD) void more(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playingIdx, queue.length, liveRefill]);

  // Очередь с телефона: мобила публикует её в POST /api/wave/publish,
  // веб подхватывает и показывает как есть (свежесть < 5 мин).
  useEffect(() => {
    if (!followPhone || !userId) return;
    let stop = false;
    const tick = async () => {
      try {
        const live = await api.waveLive(userId);
        if (stop || !live.queue?.length) return;
        if (live.age_sec != null && live.age_sec > 300) {
          setPhoneAge(live.age_sec);
          return;
        }
        setPhoneAge(live.age_sec ?? null);
        const ids = live.queue.map((t) => t.track_id).join("|");
        setQueue((prev) => {
          if (prev.map((t) => t.track_id).join("|") === ids) return prev;
          setPlayingIdx(Math.max(0, Math.min(live.current ?? 0, live.queue.length - 1)));
          return live.queue.map((t) => ({
            ...t, score: 1,
          })) as WaveTrack[];
        });
      } catch {
        /* телефона нет в сети — живём своей очередью */
      }
    };
    const id = setInterval(tick, 10000);
    void tick();
    return () => { stop = true; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [followPhone, userId]);

  // Реал-тайм от внешнего плеера: что играет в Navidrome — подсвечиваем как current.
  useEffect(() => {
    if (!followNavidrome || !userId || queue.length === 0) return;
    let stop = false;
    const tick = async () => {
      try {
        const np = await api.nowPlaying(userId, 1);
        const extId = np.playing?.track_id;
        if (stop || !extId || extId === lastSyncedExternal.current) return;
        const idx = queue.findIndex((t) => t.track_id === extId);
        if (idx >= 0) {
          lastSyncedExternal.current = extId;
          if (idx !== playingIdx) {
            pendingEvents.current.push({ track_id: extId, action: "play" });
            setPlayingIdx(idx);
          }
        } else {
          lastSyncedExternal.current = extId;
          const r = await api.waveContinue({
            user_id: userId,
            queue: queue.map((t) => t.track_id),
            current_track_id: extId,
            count: 10,
            settings: mood ? { mood } : {},
            recent_events: [...drainEvents(), { track_id: extId, action: "play" }],
          });
          if (stop) return;
          setQueue((prev) => [...prev, ...((r.tracks ?? []) as WaveTrack[])].slice(0, 100));
        }
      } catch {
        /* внешний плеер молчит — очередь живёт сама */
      }
    };
    const id = setInterval(tick, 15000);
    void tick();
    return () => { stop = true; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [followNavidrome, userId, queue.length, mood]);

  const curLook = moodLook(cur?.mood ?? "");
  const CurIcon = curLook.icon;

  return (
    <>
      <PageHeader
        title="Моя волна"
        subtitle="Что мозг поставит дальше и на чём выбор сделан — та же очередь, что уйдёт в плеер клиента"
        actions={
          <div className="flex items-center gap-2 flex-wrap">
            <select
              className="kuma-input kuma-input-inline w-48"
              value={userId}
              onChange={(e) => { setUserId(e.target.value); setQueue([]); setPlayingIdx(0); pendingEvents.current = []; }}
            >
              {ids.map((u) => (
                <option key={u.id} value={u.id}>{u.username}</option>
              ))}
            </select>
            <select
              className="kuma-input kuma-input-inline w-40"
              value={mood}
              onChange={(e) => setMood(e.target.value)}
              title="Настроение волны: авто — мозг решает сам по треку, времени суток и твоим вкусам; выбери вручную чтобы подрулить"
            >
              <option value="">Настроение: авто</option>
              {moodOptions.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
            <Button onClick={() => more(queue.length === 0)} disabled={busy || !userId}>
              {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
              {queue.length === 0 ? "Запустить волну" : "Докрутить +10"}
            </Button>
          </div>
        }
      />

      {cur && (
        <Section title="Сейчас → дальше">
          {/* Sticky now-playing: всегда сверху при скролле страницы */}
          <div className="sticky top-16 z-20">
            <Card className="!p-3 sm:!p-4 shadow-md backdrop-blur bg-[color-mix(in_srgb,var(--surface)_88%,transparent)]">
              <div className="flex items-center gap-3 min-w-0">
                <button
                  onClick={scrollToCurrent}
                  className="flex items-center gap-3 min-w-0 flex-1 text-left group"
                  title="Показать текущий в списке"
                >
                  <div className="relative shrink-0">
                    <TrackCover
                      trackId={cur.track_id}
                      coverArtId={cur.cover_art_id}
                      size={200}
                      className="w-12 h-12 sm:w-14 sm:h-14 rounded-xl"
                    />
                    <span className="absolute -bottom-1 -right-1 kuma-eq !h-3 bg-bg rounded-full px-1" aria-label="сейчас">
                      <span /><span /><span />
                    </span>
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="text-[11px] uppercase tracking-wider text-muted flex items-center gap-2">
                      <span className="inline-flex items-center gap-1">
                        <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
                        Сейчас · #{playingIdx + 1} из {queue.length}
                      </span>
                    </div>
                    <div className="truncate font-semibold leading-tight group-hover:underline underline-offset-4">
                      {cur.artist_name ? `${cur.artist_name} — ` : ""}{cur.title}
                    </div>
                    <div className="text-[11px] text-muted truncate" title={cur.reason}>
                      {cur.reason || "—"} · скор {cur.score?.toFixed(2)}
                    </div>
                  </div>
                </button>
                <span
                  className="hidden sm:inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-xs font-semibold text-white shadow shrink-0"
                  style={{ background: curLook.bg }}
                >
                  <CurIcon className="w-3.5 h-3.5" />
                  {cur.mood ?? "без настроения"}
                  {cur.energy != null && <span className="opacity-80 tabular-nums">⚡{cur.energy.toFixed(2)}</span>}
                </span>
                <ArrowRight className="w-4 h-4 text-muted shrink-0 hidden md:block" />
                {nxt && (
                  <button onClick={() => markCurrent(playingIdx + 1)} className="hidden md:flex items-center gap-2 min-w-0 max-w-[260px] text-left opacity-80 hover:opacity-100 transition-opacity" title="Следующий — клик чтобы перейти">
                    <TrackCover trackId={nxt.track_id} coverArtId={nxt.cover_art_id} size={100} className="w-9 h-9 rounded-lg" />
                    <span className="min-w-0">
                      <span className="block text-[10px] uppercase tracking-wider text-muted">Дальше</span>
                      <span className="block truncate text-sm font-medium">{nxt.artist_name ? `${nxt.artist_name} — ` : ""}{nxt.title}</span>
                    </span>
                  </button>
                )}
              </div>
              <div className="mt-2 h-1 rounded-full bg-border overflow-hidden" title={`Трек ${playingIdx + 1} из ${queue.length}`}>
                <div
                  className="h-full rounded-full transition-all"
                  style={{ width: `${queue.length ? ((playingIdx + 1) / queue.length) * 100 : 0}%`, background: curLook.bg }}
                />
              </div>
              <div className="mt-2 flex items-center gap-2 flex-wrap">
                <span
                  className="sm:hidden inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium text-white"
                  style={{ background: curLook.bg }}
                >
                  <CurIcon className="w-3 h-3" />
                  {cur.mood ?? "без настроения"}
                </span>
                <span className="text-[11px] text-muted">
                  {cur.mood && nxt?.mood
                    ? cur.mood === nxt.mood
                      ? "держим вайб"
                      : `переход: ${cur.mood} → ${nxt.mood}`
                    : "мозг подбирает по аудио + вкусу + коллаборативке"}
                </span>
                {drift?.severity && (
                  <span className="text-[11px] rounded-full border border-amber-300 px-2 py-0.5 text-amber-700 dark:text-amber-300" title={drift.temp_banned_genres.length ? `Временно мимо: ${drift.temp_banned_genres.join(", ")}` : undefined}>
                    остываем: {drift.consecutive_skips} скипа подряд — энергию вниз
                  </span>
                )}
              </div>
            </Card>
          </div>
          <Card className="!py-3 mt-3">
            <div className="flex items-center gap-4 flex-wrap text-xs text-muted">
              <label className="flex items-center gap-1.5 cursor-pointer">
                <input type="checkbox" checked={liveRefill} onChange={(e) => setLiveRefill(e.target.checked)} />
                Авто-докрутка (осталось ≤{REFILL_THRESHOLD} — добрать +10)
              </label>
              <label className="flex items-center gap-1.5 cursor-pointer" title="Мобила шлёт очередь в POST /api/wave/publish — страница показывает её как есть">
                <input type="checkbox" checked={followPhone} onChange={(e) => setFollowPhone(e.target.checked)} />
                Очередь с телефона{phoneAge != null && phoneAge <= 300 ? ` · ${phoneAge} сек назад` : ""}
              </label>
              <label className="flex items-center gap-1.5 cursor-pointer" title="Раз в 15 сек смотрим, что играет в Navidrome, и подсвечиваем">
                <input type="checkbox" checked={followNavidrome} onChange={(e) => setFollowNavidrome(e.target.checked)} />
                Подсвечивать, что играет в Navidrome
              </label>
              {busy && <span className="inline-flex items-center gap-1"><Loader2 className="w-3 h-3 animate-spin" /> мозг докладывает…</span>}
            </div>
          </Card>
        </Section>
      )}

      <Section
        title={queue.length > 0 ? `Что дальше · ${queue.length}` : "Что дальше"}
        action={
          queue.length > 0 ? (
            <button onClick={scrollToCurrent} className="kuma-pill hover:text-text transition-colors" title="Прокрутить к текущему треку">
              <ArrowDown className="w-3 h-3" /> К текущему · #{playingIdx + 1}
            </button>
          ) : undefined
        }
      >
        <Card className="relative !p-2 sm:!p-3">
          {queue.length === 0 ? (
            <div className="text-sm text-muted text-center py-8">
              Пусто — нажми «Запустить волну» или включи «Подсвечивать…» и запусти трек во внешнем плеере: страница подхватит его сама.
            </div>
          ) : (
            <>
              <div ref={listRef} className="kuma-queue-scroll relative pl-6 max-h-[620px] overflow-y-auto pr-1 py-1">
                <div className="absolute left-[9px] top-2 bottom-2 w-px bg-border" aria-hidden />
                <div className="space-y-1">
                  {queue.map((t, i) => {
                    const active = i === playingIdx;
                    const past = i < playingIdx;
                    const look = moodLook(t.mood ?? "");
                    const Icon = look.icon;
                    return (
                      <div
                        key={`${t.track_id}-${i}`}
                        ref={(el) => {
                          if (el) itemRefs.current.set(i, el);
                          else itemRefs.current.delete(i);
                        }}
                        onClick={() => markCurrent(i)}
                        className={`kuma-wave-item group relative flex items-center gap-3 rounded-xl px-2 py-2 -ml-2 cursor-pointer transition-all border ${
                          active
                            ? "bg-[color-mix(in_srgb,var(--surface)_60%,transparent)] border-[color-mix(in_srgb,var(--accent)_25%,transparent)] shadow-md"
                            : "border-transparent hover:bg-border/40 hover:border-border/60"
                        } ${past ? "opacity-55" : ""}`}
                        style={{ animationDelay: `${Math.min(i, 12) * 45}ms` }}
                        title="Клик — пометить как «сейчас играет»"
                      >
                        <span className={`absolute -left-4 w-[19px] h-[19px] rounded-full text-[10px] flex items-center justify-center border tabular-nums ${active ? "bg-text text-bg border-text font-bold" : "border-border bg-bg text-muted"}`}>
                          {i + 1}
                        </span>
                        <div className="relative shrink-0">
                          <TrackCover
                            trackId={t.track_id}
                            coverArtId={t.cover_art_id}
                            size={100}
                            className="w-10 h-10 rounded-lg"
                          />
                          <span className="absolute inset-0 rounded-lg bg-black/45 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center">
                            <Play className="w-4 h-4 text-white" />
                          </span>
                        </div>
                        <div className="min-w-0 flex-1">
                          <Link
                            href={`/track/${t.track_id}`}
                            onClick={(e) => e.stopPropagation()}
                            className="kuma-link truncate block font-medium leading-tight"
                          >
                            {t.artist_name ? `${t.artist_name} — ` : ""}{t.title}
                          </Link>
                          {/* На чём выбор сделан: причина + скор + покомпонентный скоринг */}
                          <div className="flex items-center gap-2 min-w-0">
                            <div className="text-[11px] text-muted truncate flex-1" title={t.reason}>
                              {t.reason || "—"}
                            </div>
                            <div className="hidden sm:flex items-center gap-1.5 shrink-0" title={`Скор ${t.score?.toFixed(2)}`}>
                              <div className="w-10 h-1 rounded-full bg-border overflow-hidden">
                                <div className="h-full rounded-full bg-text/60" style={{ width: `${Math.min(100, Math.max(4, (t.score ?? 0) * 100))}%` }} />
                              </div>
                              <span className="text-[10px] tabular-nums text-muted">{t.score?.toFixed(2)}</span>
                            </div>
                          </div>
                          {active && t.comp ? (
                            <div className="mt-1 flex flex-wrap gap-1">
                              {COMP_LABELS.map(({ key, label }) => {
                                const v = t.comp?.[key];
                                if (v == null) return null;
                                return (
                                  <span
                                    key={key}
                                    className="rounded-full border border-border px-1.5 py-0.5 text-[10px] tabular-nums text-muted bg-bg"
                                    title={`${label}: ${v.toFixed(2)}`}
                                  >
                                    {label} {v.toFixed(2)}
                                  </span>
                                );
                              })}
                            </div>
                          ) : null}
                        </div>
                        {t.mood && (
                          <span
                            className="hidden sm:inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[11px] font-medium text-white shrink-0 shadow-sm"
                            style={{ background: look.bg }}
                            title={[...(t.moods ?? [])].join(", ")}
                          >
                            <Icon className="w-3 h-3" />
                            {t.mood}
                          </span>
                        )}
                        {active && (
                          <span className="kuma-eq shrink-0" aria-label="сейчас">
                            <span /><span /><span />
                          </span>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
              {/* Плавающая кнопка «вернуться к текущему» — видна только когда текущий вне экрана */}
              {showJump && cur && (
                <button
                  onClick={scrollToCurrent}
                  className="kuma-jump-btn absolute bottom-4 left-1/2 -translate-x-1/2 inline-flex items-center gap-2 rounded-full px-4 py-2 text-sm font-medium shadow-lg border border-border bg-text text-bg hover:opacity-90 transition-all max-w-[90%]"
                >
                  <ArrowDown className="w-4 h-4 shrink-0" />
                  <span className="truncate">
                    К текущему · #{playingIdx + 1} — {cur.artist_name ? `${cur.artist_name} — ` : ""}{cur.title}
                  </span>
                </button>
              )}
            </>
          )}
        </Card>
      </Section>
    </>
  );
}
