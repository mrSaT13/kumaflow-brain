"use client";

import Link from "next/link";
import useSWR from "swr";
import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight, Database, RefreshCw } from "lucide-react";
import { Button, EmptyState, Input, PageHeader, Section } from "@/components/ui";
import { api, type Track } from "@/lib/api";
import { fmtDuration } from "@/lib/format";

const PAGE_SIZE = 100;

const SOURCE_LABELS: Record<string, string> = {
  navidrome: "Navidrome",
  disk: "Файлы",
  demo: "Демо",
};

export default function LibraryPage() {
  const [q, setQ] = useState("");
  const [qDebounced, setQDebounced] = useState("");
  const [genre, setGenre] = useState<string>("");
  const [source, setSource] = useState<string>("");
  const [page, setPage] = useState(0);

  useEffect(() => {
    const t = setTimeout(() => {
      setQDebounced(q);
      setPage(0);
    }, 400);
    return () => clearTimeout(t);
  }, [q]);

  useEffect(() => {
    setPage(0);
  }, [genre, source]);

  const offset = page * PAGE_SIZE;
  const { data, isLoading, mutate } = useSWR(
    ["tracks", qDebounced, genre, source, page],
    () =>
      api.listTracks({
        q: qDebounced || undefined,
        genre: genre || undefined,
        source: source || undefined,
        limit: PAGE_SIZE,
        offset,
      }),
    { keepPreviousData: true },
  );
  const { data: genresData } = useSWR("/api/library/genres", () => api.genres());
  const { data: media } = useSWR("/api/settings/media-server", () => api.getMediaServer());
  const { data: overview } = useSWR("/api/library/overview", () => api.overview());

  const isReal = Boolean(media?.url && media.url !== "http://localhost" && media.url !== "https://localhost" && media.url.trim() !== "");
  const [busy, setBusy] = useState(false);
  const { data: current } = useSWR("/api/scan/runs/current", () => api.currentRun(), { refreshInterval: 2000 });
  const isRunning = current?.current?.status === "running" || current?.current?.status === "queued";

  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

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
            ? `Подключён ${media?.url} — всего ${overview?.tracks ?? 0} треков (Navidrome: ${overview?.navidrome_tracks ?? "—"}, файлы: ${overview?.disk_tracks ?? "—"})`
            : `Все треки вашей коллекции — ${overview?.tracks ?? 0} (файлы: ${overview?.disk_tracks ?? "—"})`
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
            <select
              className="kuma-input w-36"
              value={source}
              onChange={(e) => setSource(e.target.value)}
              title="Источник треков"
            >
              <option value="">Все источники</option>
              <option value="navidrome">Navidrome</option>
              <option value="disk">Файлы с диска</option>
              <option value="demo">Демо</option>
            </select>
            {isReal ? (
              <Button onClick={syncReal} disabled={busy || isRunning}>
                <RefreshCw className={`w-4 h-4 ${busy || isRunning ? "animate-spin" : ""}`} />
                {isRunning ? "Выполняется…" : busy ? "Синхронизация…" : "Синхронизировать"}
              </Button>
            ) : (
              <Button variant="ghost" onClick={seedAndScan} disabled={busy || isRunning}>
                <Database className="w-4 h-4" /> {busy ? "Загрузка…" : "Загрузить демо"}
              </Button>
            )}
          </>
        }
      />

      <Section
        title={`Треков: ${total} · страница ${page + 1} из ${totalPages}`}
        action={
          <div className="flex items-center gap-2 text-xs">
            <button
              className="kuma-pill disabled:opacity-40"
              disabled={page <= 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
            >
              <ChevronLeft className="w-3 h-3" /> назад
            </button>
            <span className="text-muted tabular-nums">
              {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} из {total}
            </span>
            <button
              className="kuma-pill disabled:opacity-40"
              disabled={page + 1 >= totalPages}
              onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
            >
              вперёд <ChevronRight className="w-3 h-3" />
            </button>
          </div>
        }
      >
        <div className="kuma-card overflow-hidden">
          {isLoading ? (
            <div className="p-6 text-sm text-muted">Загрузка…</div>
          ) : (data?.items ?? []).length === 0 ? (
            isReal ? (
              <EmptyState
                message={
                  <>
                    В библиотеке пусто. Нажмите <span className="font-medium">«Синхронизировать»</span> — сканирование пройдёт полностью за один запуск (Navidrome + файлы с диска, если примонтирован MUSIC_DIR).
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
            <div className="overflow-x-auto">
              <table className="kuma-table w-full min-w-[900px] table-fixed">
                <thead>
                  <tr>
                    <th className="w-[28%]">Название</th>
                    <th className="w-[18%]">Артист</th>
                    <th className="w-[18%]">Альбом</th>
                    <th className="w-[10%]">Жанр</th>
                    <th className="w-[6%]">Год</th>
                    <th className="w-[8%] text-right">Длит.</th>
                    <th className="w-[6%] text-right">Просл.</th>
                    <th className="w-[6%]">Ист.</th>
                  </tr>
                </thead>
                <tbody>
                  {(data?.items ?? []).map((t: Track) => (
                    <tr key={t.id} className="hover:bg-surface/60">
                      <td>
                        <div className="flex items-center gap-2 min-w-0">
                          {/* eslint-disable-next-line @next/next/no-img-element */}
                          <img
                            src={api.trackCoverUrl(t.id)}
                            alt=""
                            loading="lazy"
                            className="w-8 h-8 rounded object-cover border border-border shrink-0"
                            onError={(e) => ((e.target as HTMLImageElement).style.display = "none")}
                          />
                          <Link href={`/track/${t.id}`} className="kuma-link truncate" title={t.title}>
                            {t.title}
                          </Link>
                        </div>
                      </td>
                      <td className="text-muted truncate" title={t.artist_name ?? ""}>{t.artist_name ?? "—"}</td>
                      <td className="text-muted truncate" title={t.album_name ?? ""}>{t.album_name ?? "—"}</td>
                      <td>{t.genre ? <span className="kuma-pill max-w-full truncate inline-block align-middle">{t.genre}</span> : "—"}</td>
                      <td className="text-muted tabular-nums">{t.year ?? "—"}</td>
                      <td className="text-right tabular-nums text-muted">
                        {fmtDuration(t.duration_sec)}
                      </td>
                      <td className="text-right tabular-nums text-muted">{t.play_count ?? 0}</td>
                      <td>
                        <span className="kuma-pill" title={t.source ?? ""}>
                          {SOURCE_LABELS[t.source ?? ""] ?? (t.source || "—")}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
        {totalPages > 1 && (
          <div className="flex items-center justify-center gap-2 mt-3 text-xs">
            <button
              className="kuma-pill disabled:opacity-40"
              disabled={page <= 0}
              onClick={() => setPage(0)}
            >
              в начало
            </button>
            <button
              className="kuma-pill disabled:opacity-40"
              disabled={page <= 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
            >
              <ChevronLeft className="w-3 h-3" /> назад
            </button>
            <span className="text-muted tabular-nums px-2">
              {page + 1} / {totalPages}
            </span>
            <button
              className="kuma-pill disabled:opacity-40"
              disabled={page + 1 >= totalPages}
              onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
            >
              вперёд <ChevronRight className="w-3 h-3" />
            </button>
          </div>
        )}
      </Section>
    </>
  );
}
