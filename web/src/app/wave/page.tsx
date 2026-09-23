"use client";

import Link from "next/link";
import useSWR from "swr";
import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowRight, Loader2, Play } from "lucide-react";
import { Button, Card, PageHeader, Section } from "@/components/ui";
import { useToast, fmtErr } from "@/components/toasts";
import { api } from "@/lib/api";
import { moodLook } from "@/lib/moodStyle";

type WaveTrack = {
  track_id: string; title: string; artist_name?: string; score: number; reason: string;
  mood?: string | null; moods?: string[]; energy?: number | null; tempo?: number | null;
};

export default function WavePage() {
  const { data: users } = useSWR("/api/users", () => api.listUsers());
  const [userId, setUserId] = useState("");
  const [mood, setMood] = useState("");
  const [queue, setQueue] = useState<WaveTrack[]>([]);
  const [playingIdx, setPlayingIdx] = useState(0);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const itemRefs = useRef<Map<number, HTMLDivElement>>(new Map());

  const ids = useMemo(() => users?.users ?? [], [users]);
  useEffect(() => {
    if (!userId && ids.length > 0) setUserId(ids[0].id);
  }, [ids, userId]);

  const { data: profile } = useSWR(userId ? ["wave-moods", userId] : null, () => api.userProfile(userId));
  const moodOptions = useMemo(() => (profile?.moods ?? []).map((m) => m.name), [profile]);

  useEffect(() => {
    const el = itemRefs.current.get(playingIdx);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [playingIdx, queue.length]);

  async function more(reset = false) {
    if (!userId) {
      toast("Выберите пользователя", "err");
      return;
    }
    setBusy(true);
    try {
      const q = reset ? [] : queue.map((t) => t.track_id);
      const r = await api.waveContinue({
        user_id: userId,
        queue: q,
        current_track_id: q.length > 0 ? q[q.length - 1] : undefined,
        count: 10,
        settings: mood ? { mood } : {},
      });
      const tracks = (r.tracks ?? []) as WaveTrack[];
      setQueue((prev) => (reset ? tracks : [...prev, ...tracks]).slice(0, 50));
      if (reset) setPlayingIdx(0);
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  const cur = queue[Math.min(playingIdx, queue.length - 1)];
  const nxt = queue[Math.min(playingIdx + 1, queue.length - 1)];
  const curLook = moodLook(cur?.mood ?? "");
  const nxtLook = moodLook(nxt?.mood ?? "");
  const CurIcon = curLook.icon;
  const NxtIcon = nxtLook.icon;

  return (
    <>
      <PageHeader
        title="Моя волна"
        subtitle="Живая очередь мозга — как у Яндекс Музыки, только докладывает наш wave-скоринг"
        actions={
          <div className="flex items-center gap-2 flex-wrap">
            <select
              className="kuma-input kuma-input-inline w-48"
              value={userId}
              onChange={(e) => { setUserId(e.target.value); setQueue([]); setPlayingIdx(0); }}
            >
              {ids.map((u) => (
                <option key={u.id} value={u.id}>{u.username}</option>
              ))}
            </select>
            <select
              className="kuma-input kuma-input-inline w-40"
              value={mood}
              onChange={(e) => setMood(e.target.value)}
              title="Настроение волны"
            >
              <option value="">Настроение: всё</option>
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
          <Card>
            <div className="flex items-center gap-3 flex-wrap">
              <span
                className="inline-flex items-center gap-2 rounded-full px-4 py-2 text-sm font-semibold text-white shadow"
                style={{ background: curLook.bg }}
              >
                <CurIcon className="w-4 h-4" />
                {cur.mood ?? "без настроения"}
                {cur.energy != null && <span className="opacity-80">· ⚡{cur.energy.toFixed(2)}</span>}
              </span>
              <ArrowRight className="w-5 h-5 text-muted kuma-wave-item" key={nxt?.track_id ?? "none"} />
              <span
                className="inline-flex items-center gap-2 rounded-full px-4 py-2 text-sm font-semibold text-white shadow"
                style={{ background: nxtLook.bg }}
              >
                <NxtIcon className="w-4 h-4" />
                {nxt?.mood ?? "—"}
                {nxt?.energy != null && <span className="opacity-80">· ⚡{nxt.energy.toFixed(2)}</span>}
              </span>
              <span className="text-xs text-muted">
                {cur.mood && nxt?.mood
                  ? cur.mood === nxt.mood
                    ? "держим вайб"
                    : `переход: ${cur.mood} → ${nxt.mood}`
                  : "мозг подбирает по аудио + вкусу + коллаборативке"}
              </span>
            </div>
          </Card>
        </Section>
      )}

      <Section title={queue.length > 0 ? `Очередь · ${queue.length}` : "Очередь"}>
        <Card>
          {queue.length === 0 ? (
            <div className="text-sm text-muted text-center py-8">
              Пусто — выбери пользователя и нажми «Запустить волну».
            </div>
          ) : (
            <div className="relative pl-6 max-h-[560px] overflow-y-auto pr-1">
              <div className="absolute left-[9px] top-2 bottom-2 w-px bg-border" aria-hidden />
              <div className="space-y-2">
                {queue.map((t, i) => {
                  const active = i === playingIdx;
                  const look = moodLook(t.mood ?? "");
                  const Icon = look.icon;
                  return (
                    <div
                      key={`${t.track_id}-${i}`}
                      ref={(el) => {
                        if (el) itemRefs.current.set(i, el);
                        else itemRefs.current.delete(i);
                      }}
                      onClick={() => setPlayingIdx(i)}
                      className={`kuma-wave-item relative flex items-center gap-3 rounded-xl px-2 py-2 -ml-2 cursor-pointer transition-all hover:bg-border/40 ${active ? "bg-border/50 shadow-sm" : ""}`}
                      style={{ animationDelay: `${Math.min(i, 12) * 45}ms` }}
                    >
                      <span className="absolute -left-4 w-[19px] h-[19px] rounded-full text-[10px] flex items-center justify-center border border-border bg-bg tabular-nums">
                        {i + 1}
                      </span>
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={api.trackCoverUrl(t.track_id, 100)}
                        alt=""
                        loading="lazy"
                        className="w-10 h-10 rounded-lg object-cover border border-border shrink-0"
                        onError={(e) => ((e.target as HTMLImageElement).style.display = "none")}
                      />
                      <div className="min-w-0 flex-1">
                        <Link
                          href={`/track/${t.track_id}`}
                          onClick={(e) => e.stopPropagation()}
                          className="kuma-link truncate block font-medium"
                        >
                          {t.artist_name ? `${t.artist_name} — ` : ""}{t.title}
                        </Link>
                        <div className="text-[11px] text-muted truncate" title={t.reason}>
                          {t.reason || "—"}
                        </div>
                      </div>
                      {t.mood && (
                        <span
                          className="hidden sm:inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[11px] font-medium text-white shrink-0"
                          style={{ background: look.bg }}
                          title={[...(t.moods ?? [])].join(", ")}
                        >
                          <Icon className="w-3 h-3" />
                          {t.mood}
                        </span>
                      )}
                      {active && (
                        <span className="kuma-eq shrink-0" aria-label="играет">
                          <span /><span /><span />
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </Card>
      </Section>
    </>
  );
}
