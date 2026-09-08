"use client";

import useSWR from "swr";
import { Badge, Card, EmptyState, PageHeader, Section } from "@/components/ui";
import { api } from "@/lib/api";
import { fmtDate, PHASE_LABELS, STATUS_LABELS, STATUS_TONE } from "@/lib/format";

export default function HistoryPage() {
  const { data } = useSWR("/api/scan/runs", () => api.listRuns(), { refreshInterval: 5000 });

  return (
    <>
      <PageHeader title="История" subtitle="Все запуски фоновых задач" />
      <Section title={`Всего запусков: ${data?.runs.length ?? 0}`}>
        {(data?.runs ?? []).length === 0 ? (
          <EmptyState message="История пуста." />
        ) : (
          <div className="kuma-card overflow-hidden">
            <table className="kuma-table w-full">
              <thead>
                <tr>
                  <th>Фаза</th>
                  <th>Статус</th>
                  <th>Прогресс</th>
                  <th>Старт</th>
                  <th>Финиш</th>
                  <th>Ошибка</th>
                </tr>
              </thead>
              <tbody>
                {(data?.runs ?? []).map((r) => (
                  <tr key={r.id}>
                    <td className="font-medium">{PHASE_LABELS[r.phase] ?? r.phase}</td>
                    <td>
                      <Badge tone={STATUS_TONE[r.status]}>{STATUS_LABELS[r.status]}</Badge>
                    </td>
                    <td className="tabular-nums text-muted">
                      {r.processed_items} / {r.total_items}
                    </td>
                    <td className="text-xs text-muted">{fmtDate(r.started_at)}</td>
                    <td className="text-xs text-muted">{fmtDate(r.finished_at)}</td>
                    <td className="text-xs text-rose-600 max-w-[240px] truncate">{r.error ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </>
  );
}
