"use client";

import useSWR from "swr";
import { useEffect, useRef, useState } from "react";
import { Activity, FileText, Play, RefreshCw, Sparkles, Zap } from "lucide-react";
import { Badge, Button, Card, EmptyState, PageHeader, Section } from "@/components/ui";
import { api, type LogLine } from "@/lib/api";
import { fmtDate, PHASE_LABELS, STATUS_LABELS, STATUS_TONE } from "@/lib/format";

function ProgressBar({ value, total }: { value: number; total: number }) {
  const pct = total > 0 ? Math.min(100, Math.round((value / total) * 100)) : 0;
  return (
    <div className="h-2 w-full rounded-full bg-border overflow-hidden">
      <div className="h-full bg-accent transition-all" style={{ width: `${pct}%` }} />
    </div>
  );
}

function LogView({ runId }: { runId: string | null }) {
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [live, setLive] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!runId) return;
    setLogs([]);
    // initial fetch
    api.runLogs(runId).then((r) => setLogs(r.logs));

    const es = new EventSource(`/api/scan/runs/${runId}/logs/stream`);
    setLive(true);
    es.onmessage = (e) => {
      try {
        const line = JSON.parse(e.data) as LogLine;
        setLogs((prev) => [...prev, line]);
      } catch {}
    };
    es.addEventListener("done", () => {
      setLive(false);
      es.close();
    });
    es.onerror = () => {
      setLive(false);
      es.close();
    };
    return () => es.close();
  }, [runId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  if (!runId) return <div className="text-sm text-muted">Выберите задачу из списка ниже чтобы увидеть логи.</div>;

  return (
    <div>
      <div className="flex items-center gap-2 mb-2">
        <FileText className="w-4 h-4 text-muted" />
        <span className="text-sm font-medium">Логи · {runId.slice(0, 8)}</span>
        {live && <Badge tone="warn">live</Badge>}
      </div>
      <div className="kuma-card !p-0 overflow-hidden">
        <pre className="text-xs leading-relaxed max-h-96 overflow-auto p-4 font-mono whitespace-pre-wrap">
          {logs.length === 0 ? "логов пока нет…" : logs.map((l) => `[${fmtDate(l.created_at)}] ${l.level.toUpperCase().padEnd(5)}  ${l.message}`).join("\n")}
          <div ref={bottomRef} />
        </pre>
      </div>
    </div>
  );
}

export default function ScansPage() {
  const { data: current, mutate: refreshCurrent } = useSWR("/api/scan/runs/current", () => api.currentRun(), {
    refreshInterval: 2000,
  });
  const { data: runs, mutate: refreshRuns } = useSWR("/api/scan/runs", () => api.listRuns(), {
    refreshInterval: 3000,
  });
  const [selectedRun, setSelectedRun] = useState<string | null>(null);

  async function start(kind: "library" | "analysis" | "lyrics" | "clusters") {
    let res: { run_id: string };
    if (kind === "library") res = await api.startLibraryScan();
    else if (kind === "analysis") res = await api.startAnalysis();
    else if (kind === "lyrics") res = await api.startLyrics();
    else res = await api.startClusters();
    setSelectedRun(res.run_id);
    refreshRuns();
    refreshCurrent();
  }

  return (
    <>
      <PageHeader
        title="Задачи и логи"
        subtitle="История и live-статус всех фоновых задач"
        actions={
          <div className="flex items-center gap-2 flex-wrap">
            <Button variant="ghost" onClick={() => start("library")}>
              <RefreshCw className="w-4 h-4" /> Библиотека
            </Button>
            <Button variant="ghost" onClick={() => start("analysis")}>
              <Sparkles className="w-4 h-4" /> Sonic
            </Button>
            <Button variant="ghost" onClick={() => start("lyrics")}>
              <FileText className="w-4 h-4" /> Тексты + AI
            </Button>
            <Button onClick={() => start("clusters")}>
              <Zap className="w-4 h-4" /> Кластеры
            </Button>
          </div>
        }
      />

      <Section title="Текущая задача">
        <Card>
          {current?.current ? (
            <div className="space-y-3">
              <div className="flex items-center gap-2 text-sm flex-wrap">
                <Activity className="w-4 h-4 text-muted" />
                <span className="font-medium">{PHASE_LABELS[current.current.phase] ?? current.current.phase}</span>
                <Badge tone={STATUS_TONE[current.current.status]}>{STATUS_LABELS[current.current.status]}</Badge>
                <span className="ml-auto text-xs text-muted">старт {fmtDate(current.current.started_at)}</span>
              </div>
              <ProgressBar value={current.current.processed_items} total={current.current.total_items} />
              <div className="text-xs text-muted">
                {current.current.processed_items} / {current.current.total_items}
              </div>
            </div>
          ) : (
            <EmptyState message="Нет активных задач." />
          )}
        </Card>
      </Section>

      <Section title="История">
        <div className="kuma-card overflow-hidden">
          {(runs?.runs ?? []).length === 0 ? (
            <EmptyState message="Задач ещё не было." />
          ) : (
            <table className="kuma-table w-full">
              <thead>
                <tr>
                  <th>Фаза</th>
                  <th>Статус</th>
                  <th>Прогресс</th>
                  <th>Старт</th>
                  <th>Финиш</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {runs!.runs.map((r) => (
                  <tr key={r.id} className={selectedRun === r.id ? "bg-surface" : ""}>
                    <td className="font-medium">{PHASE_LABELS[r.phase] ?? r.phase}</td>
                    <td>
                      <Badge tone={STATUS_TONE[r.status]}>{STATUS_LABELS[r.status]}</Badge>
                    </td>
                    <td className="tabular-nums text-muted">
                      {r.processed_items} / {r.total_items}
                    </td>
                    <td className="text-muted text-xs">{fmtDate(r.started_at)}</td>
                    <td className="text-muted text-xs">{fmtDate(r.finished_at)}</td>
                    <td className="text-right">
                      <button className="kuma-pill hover:text-text" onClick={() => setSelectedRun(r.id)}>
                        <FileText className="w-3 h-3" /> логи
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </Section>

      <Section title="Логи">
        <LogView runId={selectedRun} />
      </Section>
    </>
  );
}
