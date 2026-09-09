"use client";

import Link from "next/link";
import useSWR from "swr";
import { useState } from "react";
import { Database, RefreshCw } from "lucide-react";
import { Button, EmptyState, Input, PageHeader, Section } from "@/components/ui";
import { api, type Track } from "@/lib/api";
import { fmtDuration } from "@/lib/format";

export default function LibraryPage() {
  const [q, setQ] = useState("");
  const [genre, setGenre] = useState<string>("");
  const { data, isLoading, mutate } = useSWR(
    ["tracks", q, genre],
    () => api.listTracks({ q: q || undefined, genre: genre || undefined, limit: 200 }),
    { keepPreviousData: true },
  );
  const { data: genresData } = useSWR("/api/library/genres", () => api.genres());
  const { data: media } = useSWR("/api/settings/media-server", () => api.getMediaServer());
  const { data: overview } = useSWR("/api/library/overview", () => api.overview());

  const isReal = Boolean(media?.url && media.url !== "http://localhost" && media.url !== "https://localhost" && media.url.trim() !== "");
  const [busy, setBusy] = useState(false);
  const { data: current } = useSWR("/api/scan/runs/current", () => api.currentRun(), { refreshInterval: 2000 });
  const isRunning = current?.current?.status === "running" || current?.current?.status === "queued";

  async function syncReal() {
    if (isRunning) {
      alert(`Уже выполняется: ${current?.current?.phase} — подождите`);
      return;
    }
    setBusy(true);
    try {
      await api.startLibraryScan();
      setTimeout(() => mutate(), 1500);
      setTimeout(() => mutate(), 4000);
    } catch (e: unknown) {
      const msg = String(e);
      if (msg.includes("409")) alert("Задача уже выполняется — подождите завершения");
      else alert(msg);
    } finally {
      setBusy(false);
    }
  }

  async function seedAndScan() {
    setBusy(true);
    try {
      await api.startLibraryScan();
      setTimeout(() => mutate(), 800);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Библиотека"
        subtitle={
          isReal
            ? `Подключён ${media?.url} — ${overview?.tracks ?? 0} треков`
            : "Все треки вашей коллекции"
        }
        actions={
          <>
            <Input
              className="w-64"
              placeholder="Поиск по названию, артисту…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
            <select
              className="kuma-input w-40"
              value={genre}
              onChange={(e) => setGenre(e.target.value)}
            >
              <option value="">Все жанры</option>
              {(genresData?.genres ?? []).map((g) => (
                <option key={g} value={g}>
                  {g}
                </option>
              ))}
            </select>
            {isReal ? (
              <Button onClick={syncReal} disabled={busy || isRunning}>
                <RefreshCw className={`w-4 h-4 ${busy || isRunning ? "animate-spin" : ""}`} />
                {isRunning ? "Выполняется…" : busy ? "Синхронизация…" : "Синхронизировать с Navidrome"}
              </Button>
            ) : (
              <Button variant="ghost" onClick={seedAndScan} disabled={busy || isRunning}>
                <Database className="w-4 h-4" /> {busy ? "Загрузка…" : "Загрузить демо"}
              </Button>
            )}
          </>
        }
      />

      <Section title={`Треков: ${data?.total ?? 0}`}>
        <div className="kuma-card overflow-hidden">
          {isLoading ? (
            <div className="p-6 text-sm text-muted">Загрузка…</div>
          ) : (data?.items ?? []).length === 0 ? (
            isReal ? (
              <EmptyState
                message={
                  <>
                    В библиотеке пусто. Нажмите <span className="font-medium">«Синхронизировать с Navidrome»</span> — сканирование пройдёт полностью за один запуск (включая файлы с диска, если примонтирован MUSIC_DIR).
                  </>
                }
              />
            ) : (
              <EmptyState
                message={
                  <>
                    В библиотеке пусто. Нажмите <span className="font-medium">«Загрузить демо»</span>, чтобы добавить тестовые треки, или подключите Navidrome в Настройках.
                  </>
                }
              />
            )
          ) : (
            <table className="kuma-table w-full">
              <thead>
                <tr>
                  <th>Название</th>
                  <th>Артист</th>
                  <th>Альбом</th>
                  <th>Жанр</th>
                  <th>Год</th>
                  <th className="text-right">Длительность</th>
                  <th className="text-right">Прослушиваний</th>
                </tr>
              </thead>
              <tbody>
                {(data?.items ?? []).map((t: Track) => (
                  <tr key={t.id} className="hover:bg-surface/60">
                    <td>
                      <div className="flex items-center gap-2">
                        {/* eslint-disable-next-line @next/next/no-img-element */}
                        <img
                          src={api.trackCoverUrl(t.id)}
                          alt=""
                          loading="lazy"
                          className="w-8 h-8 rounded object-cover border border-border shrink-0"
                          onError={(e) => ((e.target as HTMLImageElement).style.display = "none")}
                        />
                        <Link href={`/track/${t.id}`} className="kuma-link">
                          {t.title}
                        </Link>
                      </div>
                    </td>
                    <td className="text-muted">{t.artist_name ?? "—"}</td>
                    <td className="text-muted">{t.album_name ?? "—"}</td>
                    <td>{t.genre ? <span className="kuma-pill">{t.genre}</span> : "—"}</td>
                    <td className="text-muted">{t.year ?? "—"}</td>
                    <td className="text-right tabular-nums text-muted">
                      {fmtDuration(t.duration_sec)}
                    </td>
                    <td className="text-right tabular-nums text-muted">{t.play_count ?? 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </Section>
    </>
  );
}
