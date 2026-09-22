"use client";

import useSWR from "swr";
import Link from "next/link";
import { Activity, Disc3, ListMusic, Sparkles, Users, Library, Scan } from "lucide-react";
import { Badge, Card, PageHeader, Section, Stat, Button, CountUp } from "@/components/ui";
import { api } from "@/lib/api";
import { fmtNumber, PHASE_LABELS, STATUS_LABELS, STATUS_TONE, fmtDate } from "@/lib/format";

export default function HomePage() {
  const { data: overview } = useSWR("/api/library/overview", () => api.overview());
  const { data: runs } = useSWR("/api/scan/runs", () => api.listRuns(), { refreshInterval: 3000 });
  const { data: current } = useSWR("/api/scan/runs/current", () => api.currentRun(), {
    refreshInterval: 2000,
  });

  const analyzed = overview?.analyzed_tracks ?? 0;
  const analyzedPct = overview?.tracks ? Math.round((analyzed / overview.tracks) * 100) : 0;

  return (
    <>
      <PageHeader
        title="Главная"
        subtitle="Общий обзор вашей библиотеки и активных задач"
      />

      <Section title="Обзор">
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 kuma-fade-in">
          <Stat label="Треков" value={<CountUp value={overview?.tracks ?? 0} format={fmtNumber} />} icon={<Library className="w-3 h-3" />}
            hint={overview ? `проанализировано ${fmtNumber(analyzed)} · ${analyzedPct}%` : undefined} />
          <Stat label="Альбомов" value={<CountUp value={overview?.albums ?? 0} format={fmtNumber} />} />
          <Stat label="Артистов" value={<CountUp value={overview?.artists ?? 0} format={fmtNumber} />} />
          <Stat
            label="Пользователей"
            value={<CountUp value={overview?.users ?? 0} format={fmtNumber} />}
            hint="источник для коллаборативной фильтрации"
            icon={<Users className="w-3 h-3" />}
          />
        </div>
      </Section>

      <Section title="Текущая задача">
        <Card>
          {current?.current ? (
            <div className="flex items-center gap-3 flex-wrap">
              <Activity className="w-4 h-4 text-muted" />
              <div className="text-sm">
                <span className="font-medium">{PHASE_LABELS[current.current.phase] ?? current.current.phase}</span>
              </div>
              <Badge tone={STATUS_TONE[current.current.status]}>
                {STATUS_LABELS[current.current.status]}
              </Badge>
              <div className="ml-auto text-xs text-muted">
                {current.current.processed_items} / {current.current.total_items}
              </div>
              <Link href="/scans" className="kuma-link text-sm">Открыть логи →</Link>
            </div>
          ) : (
            <div className="text-sm text-muted flex items-center gap-2">
              <Sparkles className="w-4 h-4" /> Нет активных задач. Запустите сканирование или анализ во вкладке «Задачи и логи».
            </div>
          )}
        </Card>
      </Section>

      <Section title="Быстрые действия">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Button onClick={() => api.startLibraryScan().then(() => location.reload())}>
            <Library className="w-4 h-4" /> Сканировать
          </Button>
          <Button variant="ghost" onClick={() => api.startAnalysis()}>
            <Sparkles className="w-4 h-4" /> Sonic-анализ
          </Button>
          <Button variant="ghost" onClick={() => api.startLyrics()}>
            <Disc3 className="w-4 h-4" /> Тексты + AI
          </Button>
          <Button variant="ghost" onClick={() => api.generateDailyPlaylist(30)}>
            <ListMusic className="w-4 h-4" /> Создать плейлист
          </Button>
        </div>
      </Section>

      <Section title="Последние задачи">
        <Card>
          <div className="divide-y divide-border">
            {(runs?.runs ?? []).slice(0, 8).map((r) => (
              <div key={r.id} className="py-3 flex items-center gap-3 text-sm">
                <Scan className="w-4 h-4 text-muted" />
                <span className="font-medium">{PHASE_LABELS[r.phase] ?? r.phase}</span>
                <Badge tone={STATUS_TONE[r.status]}>{STATUS_LABELS[r.status]}</Badge>
                <span className="ml-auto text-xs text-muted">{fmtDate(r.started_at)}</span>
              </div>
            ))}
            {(!runs?.runs || runs.runs.length === 0) && (
              <div className="py-6 text-sm text-muted">Задач ещё не было.</div>
            )}
          </div>
        </Card>
      </Section>
    </>
  );
}
