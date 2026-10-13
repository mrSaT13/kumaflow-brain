"use client";

import useSWR from "swr";
import { useState } from "react";
import { RefreshCw } from "lucide-react";
import { Badge, Button, Card } from "@/components/ui";
import { useToast, fmtErr } from "@/components/toasts";
import { api } from "@/lib/api";

/** Вкладка «О программе»: версия мозга, что нового, проверка обновлений.
 *  Проверка — POST /api/updates/check (мозг сверяется с GitHub сам,
 *  о новой версии пишет в колокольчик; повторных спам-уведомлений нет). */
export default function About() {
  const { data, mutate } = useSWR("/api/updates", () => api.updates());
  const [busy, setBusy] = useState(false);
  const [check, setCheck] = useState<{ latest: string | null; update_available: boolean; note?: string } | null>(null);
  const toast = useToast();

  async function onCheck() {
    setBusy(true);
    try {
      const r = await api.checkUpdates();
      setCheck(r);
      mutate();
      if (r.latest === null) {
        toast(r.note ?? "GitHub недоступен — попробуй позже", "info");
      } else if (r.update_available) {
        toast(`Доступна версия ${r.latest} (стоит ${r.current}) — детали в колокольчике`, "ok");
      } else {
        toast(`У тебя последняя версия (${r.current}) ✓`, "ok");
      }
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  const current = data?.current ?? "—";
  const latest = check?.latest;
  const available = check?.update_available ?? false;

  return (
    <>
      <Card>
        <div className="flex items-center gap-2 flex-wrap mb-2">
          <span className="font-medium text-sm">KumaFlow Brain</span>
          <Badge tone="default">{current}</Badge>
          {latest != null && (
            available
              ? <Badge tone="ok">доступна {latest}</Badge>
              : <Badge tone="default">последняя ✓</Badge>
          )}
        </div>
        <div className="text-xs text-muted mb-3">
          Мозг проверяет обновления сам раз в день (крон check_updates) и пишет в колокольчик.
          Кнопка — та же проверка, но сразу.
        </div>
        <Button onClick={onCheck} disabled={busy}>
          <RefreshCw className="w-4 h-4" /> {busy ? "Проверяю…" : "Проверить обновления"}
        </Button>
      </Card>
      <Card>
        <div className="font-medium text-sm mb-2">Что нового</div>
        {!data ? (
          <div className="text-xs text-muted">Загрузка…</div>
        ) : data.changelog ? (
          <pre className="text-xs text-muted whitespace-pre-wrap max-h-96 overflow-auto">{data.changelog}</pre>
        ) : (
          <div className="text-xs text-muted">Чейнджлог недоступен (нет CHANGELOG.md на сервере).</div>
        )}
      </Card>
    </>
  );
}
