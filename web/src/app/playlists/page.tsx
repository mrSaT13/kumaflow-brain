"use client";

import useSWR from "swr";
import Link from "next/link";
import { useState } from "react";
import { ListMusic, RefreshCw, Sparkles, Trash2, Wand2 } from "lucide-react";
import { Badge, Button, Card, EmptyState, Input, PageHeader, Section } from "@/components/ui";
import { api } from "@/lib/api";
import { fmtDate } from "@/lib/format";

export default function PlaylistsPage() {
  const { data, mutate } = useSWR("/api/playlists", () => api.listPlaylists(), { refreshInterval: 4000 });
  const { data: usersData } = useSWR("/api/users", () => api.listUsers());
  const [busy, setBusy] = useState(false);
  const [coldUser, setColdUser] = useState("");
  const [coldN, setColdN] = useState(30);
  const [aiQuery, setAiQuery] = useState("");
  const [lastSteps, setLastSteps] = useState<{ step: number; name: string; items: number }[] | null>(null);

  async function generate() {
    setBusy(true);
    try {
      const r = await api.generateDailyPlaylist(coldN || 30, coldUser || undefined);
      setLastSteps(r.steps ?? null);
      mutate();
    } catch (e: unknown) {
      alert(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function coldStartOnly() {
    setBusy(true);
    try {
      const r = await api.coldStart(coldN || 30);
      setLastSteps(r.steps ?? null);
      alert(`Cold-start: кандидатов ${r.tracks.length} (шаги: ${(r.steps ?? []).map((s) => `${s.name}=${s.items}`).join(", ")}). Чтобы сохранить их плейлистом — нажмите «Пройти холодный старт».`);
    } catch (e: unknown) {
      alert(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function aiMix() {
    if (!aiQuery.trim()) return alert("Опишите настроение/жанр — например «вечерний лоуфай для работы»");
    setBusy(true);
    try {
      const r = await api.aiGenerate(aiQuery.trim(), coldN || 30, coldUser || undefined);
      mutate();
      alert(`Готово: «${r.name}» — треков ${r.tracks}${r.from_fallback ? " (без AI, keyword-подбор)" : ""}. Откройте плейлист ниже.`);
    } catch (e: unknown) {
      alert(String(e));
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
            {busy ? "Генерирую…" : "Пройти холодный старт"}
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
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mt-4">
            <label className="block">
              <div className="text-xs text-muted mb-1">Пользователь (персонально)</div>
              <select className="kuma-input" value={coldUser} onChange={(e) => setColdUser(e.target.value)}>
                <option value="">Общий (вся библиотека)</option>
                {(usersData?.users ?? []).map((u) => (
                  <option key={u.id} value={u.id}>{u.username}</option>
                ))}
              </select>
            </label>
            <label className="block">
              <div className="text-xs text-muted mb-1">Треков</div>
              <select className="kuma-input" value={String(coldN)} onChange={(e) => setColdN(Number(e.target.value))}>
                {[15, 30, 50].map((n) => (
                  <option key={n} value={n}>{n}</option>
                ))}
              </select>
            </label>
            <div className="flex items-end">
              <div className="text-xs text-muted">Без пользователя — по глобальным счётчикам. С пользователем — по его лайкам (импорт на странице «Пользователи»).</div>
            </div>
          </div>
          {lastSteps && (
            <div className="mt-3 flex gap-2 text-xs flex-wrap">
              {lastSteps.map((s) => (
                <Badge key={s.step}>
                  шаг {s.step}: {s.name} — {s.items}
                </Badge>
              ))}
            </div>
          )}
          <div className="mt-3 flex gap-2 flex-wrap">
            <Button
              variant="ghost"
              onClick={coldStartOnly}
              disabled={busy}
            >
              <Sparkles className="w-4 h-4" /> Тест cold-start
            </Button>
            <Button
              variant="ghost"
              onClick={async () => {
                setBusy(true);
                try {
                  const r = await api.buildClusters();
                  alert(`Кластеризация запущена (задача ${r.run_id.slice(0, 8)}). Следите в «Задачи и логи».`);
                } catch (e: unknown) {
                  alert(String(e));
                } finally {
                  setBusy(false);
                }
              }}
              disabled={busy}
            >
              Пересобрать кластеры
            </Button>
          </div>
        </Card>
      </Section>

      <Section title="AI-микс по описанию">
        <Card>
          <div className="flex flex-col md:flex-row gap-2">
            <Input
              className="flex-1"
              value={aiQuery}
              onChange={(e) => setAiQuery(e.target.value)}
              placeholder="Например: вечерний лоуфай для работы, без вокала"
            />
            <Button onClick={aiMix} disabled={busy}>
              <Wand2 className="w-4 h-4" /> Создать AI-микс
            </Button>
          </div>
          <div className="text-xs text-muted mt-2">Использует настроенного AI-провайдера (Настройки → AI). Без AI — подберёт по ключевым словам.</div>
        </Card>
      </Section>

      <Section title={`Всего плейлистов: ${data?.playlists.length ?? 0}`}>
        {(data?.playlists ?? []).length === 0 ? (
          <EmptyState message="Плейлистов ещё нет. Нажмите «Пройти холодный старт»." />
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
