"use client";

import useSWR from "swr";
import Link from "next/link";
import { useState } from "react";
import { ListMusic, RefreshCw, Sparkles, Trash2 } from "lucide-react";
import { Badge, Button, Card, EmptyState, PageHeader, Section } from "@/components/ui";
import { api } from "@/lib/api";
import { fmtDate } from "@/lib/format";

export default function PlaylistsPage() {
  const { data, mutate } = useSWR("/api/playlists", () => api.listPlaylists(), { refreshInterval: 4000 });
  const [busy, setBusy] = useState(false);
  const [lastSteps, setLastSteps] = useState<{ step: number; name: string; items: number }[] | null>(null);

  async function generate() {
    setBusy(true);
    try {
      const r = await api.generateDailyPlaylist(30);
      setLastSteps(r.steps);
      mutate();
    } finally {
      setBusy(false);
    }
  }

  async function del(id: string) {
    if (!confirm("Удалить плейлист?")) return;
    await api.deletePlaylist(id);
    mutate();
  }

  return (
    <>
      <PageHeader
        title="Плейлисты"
        subtitle="Ежедневный плейлист пересоздаётся автоматически (если уже есть — удаляется и делается заново). Cold-start в 3 шага."
        actions={
          <Button onClick={generate} disabled={busy}>
            <RefreshCw className={`w-4 h-4 ${busy ? "animate-spin" : ""}`} />
            {busy ? "Генерирую…" : "Сгенерировать ежедневный"}
          </Button>
        }
      />

      <Section title="Алгоритм">
        <Card>
          <ol className="list-decimal list-inside text-sm space-y-1 text-muted">
            <li>
              <span className="text-text font-medium">Сигнал</span> — собираем жанры/артистов по прослушиваниям + избранному + оценкам.
            </li>
            <li>
              <span className="text-text font-medium">Кандидаты</span> — расширяем похожими артистами (кластера/жанры).
            </li>
            <li>
              <span className="text-text font-medium">Диверсификация</span> — MMR-баланс чтобы не зацикливаться на одном, 30 треков.
            </li>
          </ol>
          {lastSteps && (
            <div className="mt-3 flex gap-2 text-xs">
              {lastSteps.map((s) => (
                <Badge key={s.step}>
                  шаг {s.step}: {s.name} — {s.items}
                </Badge>
              ))}
            </div>
          )}
          <div className="mt-3 flex gap-2">
            <Button
              variant="ghost"
              onClick={async () => {
                await api.coldStart(15);
                alert("Cold-start вернул 15 кандидатов (см консоль)");
              }}
            >
              <Sparkles className="w-4 h-4" /> Тест cold-start
            </Button>
            <Button
              variant="ghost"
              onClick={async () => {
                setBusy(true);
                await fetch("/api/analysis/clusters/build", { method: "POST" });
                setBusy(false);
                alert("Кластеры пересобраны");
              }}
            >
              Пересобрать кластеры
            </Button>
          </div>
        </Card>
      </Section>

      <Section title={`Всего плейлистов: ${data?.playlists.length ?? 0}`}>
        {(data?.playlists ?? []).length === 0 ? (
          <EmptyState message="Плейлистов ещё нет. Сгенерируйте ежедневный." />
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {(data?.playlists ?? []).map((p) => (
              <Card key={p.id} className="flex flex-col gap-3">
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <div className="font-medium flex items-center gap-2">
                      <ListMusic className="w-4 h-4 text-muted" />
                      {p.name}
                    </div>
                    <div className="text-xs text-muted mt-1">
                      {p.is_auto_generated ? "ежедневный" : "обычный"} · {p.track_count} треков · {fmtDate(p.created_at)}
                    </div>
                  </div>
                  <Badge tone={p.is_auto_generated ? "info" : "default"}>
                    {p.is_auto_generated ? "auto" : "manual"}
                  </Badge>
                </div>
                <div className="flex gap-2">
                  <Link href={`/playlists/${p.id}`} className="kuma-btn kuma-btn-ghost text-sm">
                    Открыть
                  </Link>
                  <button className="kuma-pill hover:text-text" onClick={() => del(p.id)}>
                    <Trash2 className="w-3 h-3" /> удалить
                  </button>
                </div>
              </Card>
            ))}
          </div>
        )}
      </Section>
    </>
  );
}
