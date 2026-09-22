"use client";

import useSWR from "swr";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { FileText, Sparkles } from "lucide-react";
import { Button, Card, EmptyState, PageHeader, Section, Skeleton } from "@/components/ui";
import { api, type Track } from "@/lib/api";
import { fmtDuration } from "@/lib/format";
import { moodLook } from "@/lib/moodStyle";

export default function TrackPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const { data, error, isLoading, mutate } = useSWR(["track", id], () => api.getTrack(id));
  const { data: recs } = useSWR(data ? ["rec", id] : null, () => api.recommendByTrack(id));
  const [busy, setBusy] = useState<"sonic" | "lyrics" | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function analyzeNow() {
    setBusy("sonic");
    setNotice(null);
    try {
      const r = await api.analyzeTrack(id);
      setNotice(`Sonic-анализ запущен (задача ${r.run_id.slice(0, 8)}). Обновите страницу через минуту.`);
    } catch (e: unknown) {
      setNotice(`Ошибка: ${String(e)}`);
    } finally {
      setBusy(null);
    }
  }

  async function lyricsNow() {
    setBusy("lyrics");
    setNotice(null);
    try {
      const r = await api.fetchTrackLyrics(id);
      if (!r.ok) setNotice(`Текст не найден: ${r.error ?? "LRCLIB ничего не вернул"}`);
      else {
        setNotice("Текст загружен.");
        mutate();
      }
    } catch (e: unknown) {
      setNotice(`Ошибка: ${String(e)}`);
    } finally {
      setBusy(null);
    }
  }

  if (isLoading) return (
    <div className="space-y-4">
      <Skeleton className="h-16" />
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Skeleton className="h-64" />
        <Skeleton className="h-64" />
        <Skeleton className="h-64" />
      </div>
    </div>
  );
  if (error || !data) return <EmptyState message="Трек не найден или бэкенд недоступен." />;

  const f = data.features ?? {};

  return (
    <>
      <PageHeader
        title={data.title}
        subtitle={[data.artist_name, data.album_name, data.year].filter(Boolean).join(" · ")}
        actions={
          <div className="flex items-center gap-2 flex-wrap">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={api.trackCoverUrl(id, 80)}
              alt="Обложка"
              loading="lazy"
              decoding="async"
              className="w-10 h-10 rounded-lg object-cover border border-border"
              onError={(e) => ((e.target as HTMLImageElement).style.display = "none")}
            />
            <Button variant="ghost" onClick={analyzeNow} disabled={busy !== null}>
              <Sparkles className="w-4 h-4" /> {busy === "sonic" ? "Анализ…" : "Проанализировать трек"}
            </Button>
            {!data.lyrics && (
              <Button variant="ghost" onClick={lyricsNow} disabled={busy !== null}>
                <FileText className="w-4 h-4" /> {busy === "lyrics" ? "Ищу…" : "Загрузить текст"}
              </Button>
            )}
          </div>
        }
      />
      {notice && (
        <div className="mb-4 text-sm p-3 rounded-lg border border-border bg-surface">{notice}</div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Card className="lg:col-span-1">
          <div className="flex items-start gap-3 mb-3">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={api.trackCoverUrl(id, 300)}
              alt="Обложка трека"
              loading="lazy"
              decoding="async"
              className="w-24 h-24 rounded-xl object-cover border border-border shrink-0"
              onError={(e) => ((e.target as HTMLImageElement).style.display = "none")}
            />
            <div className="text-xs uppercase tracking-wider text-muted pt-1">Метаданные</div>
          </div>
          <dl className="text-sm space-y-2">
            <Row k="Артист" v={data.artist_name} />
            <Row k="Альбом" v={data.album_name} />
            <Row k="Жанр" v={data.genre} />
            <Row k="Год" v={data.year} />
            <Row k="Длительность" v={fmtDuration(data.duration_sec)} />
            <Row k="Прослушиваний" v={data.play_count} />
            <Row k="Оценка" v={data.rating} />
            <Row k="Кластер" v={data.cluster ? `#${data.cluster.id} (${data.cluster.algorithm})` : "—"} />
          </dl>
        </Card>

        <Card className="lg:col-span-2">
          <div className="text-xs uppercase tracking-wider text-muted mb-2">Sonic-фичи</div>
          {Object.keys(f).length === 0 ? (
            <EmptyState message="Трек ещё не проанализирован. Нажмите «Проанализировать трек» выше или запустите sonic-анализ на странице «Задачи и логи»." />
          ) : (
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
              {Object.entries(f).map(([k, v]) => (
                <div key={k} className="kuma-card !p-3">
                  <div className="text-[11px] text-muted">{k}</div>
                  <div className="text-sm font-medium tabular-nums mt-1">
                    {typeof v === "number" ? v.toFixed(3) : String(v)}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card className="lg:col-span-1">
          <div className="text-xs uppercase tracking-wider text-muted mb-3">Настроение</div>
          {data.moods && data.moods.length ? (
            <>
              <div className="flex flex-wrap gap-2 kuma-fade-in">
                {data.moods.map((m) => {
                  const look = moodLook(m);
                  const MIcon = look.icon;
                  return (
                    <span
                      key={m}
                      className="inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm font-medium text-white shadow-sm"
                      style={{ background: look.bg }}
                    >
                      <MIcon className="w-4 h-4" />
                      {m}
                    </span>
                  );
                })}
              </div>
              <MoodMeters features={f} />
            </>
          ) : (
            <EmptyState message="Настроение появится после AI-анализа текста." />
          )}
        </Card>

        <Card className="lg:col-span-2">
          <div className="text-xs uppercase tracking-wider text-muted mb-2">Текст песни</div>
          {data.lyrics ? (
            <>
              <pre className="whitespace-pre-wrap text-sm leading-relaxed font-sans max-h-96 overflow-auto">
                {data.lyrics.text}
              </pre>
              <div className="text-[11px] text-muted mt-2">
                Источник: {data.lyrics.provider} {data.lyrics.source_url ? `· ${data.lyrics.source_url}` : ""}
              </div>
            </>
          ) : (
            <EmptyState message="Текст ещё не загружен. Нажмите «Загрузить текст» выше или запустите загрузку текстов на странице «Задачи и логи»." />
          )}
        </Card>

        <Card className="lg:col-span-3">
          <div className="text-xs uppercase tracking-wider text-muted mb-3">Похожие треки (content-based, косинус по фичам)</div>
          {recs?.items && recs.items.length > 0 ? (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2">
              {recs.items.slice(0, 9).map((t: Track & { score?: number }) => (
                <Link
                  key={t.id}
                  href={`/track/${t.id}`}
                  className="kuma-card !p-3 hover:bg-surface flex items-center gap-3"
                >
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium truncate">{t.title}</div>
                    <div className="text-xs text-muted truncate">{t.artist_name ?? "—"}</div>
                  </div>
                  {typeof t.score === "number" && (
                    <div className="text-xs tabular-nums text-muted">{(t.score * 100).toFixed(0)}%</div>
                  )}
                </Link>
              ))}
            </div>
          ) : (
            <div className="text-sm text-muted">Похожих пока нет — нужен sonic-анализ большего числа треков.</div>
          )}
        </Card>
      </div>
    </>
  );
}

function MoodMeters({ features }: { features: Record<string, number | string | null> }) {
  const meters = [
    { key: "energy", label: "Энергия", bar: "linear-gradient(90deg,#f59e0b,#ef4444)" },
    { key: "valence", label: "Позитив", bar: "linear-gradient(90deg,#34d399,#fbbf24)" },
    { key: "danceability", label: "Танцевальность", bar: "linear-gradient(90deg,#ec4899,#8b5cf6)" },
  ].map((m) => {
    const raw = features[m.key];
    return { ...m, value: typeof raw === "number" ? Math.min(1, Math.max(0, raw)) : null };
  }).filter((m) => m.value !== null);
  if (meters.length === 0) return null;
  return (
    <div className="mt-4 space-y-2.5">
      <div className="text-xs uppercase tracking-wider text-muted">Характер трека</div>
      {meters.map((m) => (
        <div key={m.key}>
          <div className="flex items-center justify-between text-xs mb-1">
            <span className="text-muted">{m.label}</span>
            <span className="tabular-nums font-medium">{Math.round((m.value ?? 0) * 100)}%</span>
          </div>
          <div className="h-2 rounded-full bg-border overflow-hidden">
            <div className="h-full rounded-full transition-all" style={{ width: `${Math.round((m.value ?? 0) * 100)}%`, background: m.bar }} />
          </div>
        </div>
      ))}
    </div>
  );
}

function Row({ k, v }: { k: string; v: unknown }) {
  return (
    <div className="flex items-center justify-between">
      <dt className="text-muted">{k}</dt>
      <dd>{v === undefined || v === null || v === "" ? "—" : String(v)}</dd>
    </div>
  );
}
