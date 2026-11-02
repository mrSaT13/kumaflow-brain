"use client";

import { useState } from "react";
import useSWR from "swr";
import { Copy, KeyRound, Plus, Trash2 } from "lucide-react";
import { Badge, Button, Card } from "@/components/ui";
import { useToast, fmtErr } from "@/components/toasts";
import { api } from "@/lib/api";
import { fmtDate } from "@/lib/format";

const PRESETS: { key: string; label: string; hint: string }[] = [
  { key: "mobile", label: "Плеер (волна + синк + плейлисты)", hint: "для телефона или ПК-плеера" },
  { key: "wave", label: "Только волна", hint: "без синка вкусов" },
];

/** «Мои токены» в СВОЁМ профиле: обычный юзер сам выпускает себе токен
 *  для плеера, без админа. Владелец всегда = сам, скоуп admin недоступен. */
export default function MyTokens() {
  const { data, error, isLoading, mutate } = useSWR("/api/me/tokens", () => api.myTokens());
  const [name, setName] = useState("Мой телефон");
  const [preset, setPreset] = useState("mobile");
  const [busy, setBusy] = useState(false);
  const [fresh, setFresh] = useState<{ name: string; token: string } | null>(null);
  const toast = useToast();

  if (isLoading) return <div className="text-sm text-muted">Загрузка…</div>;
  if (error || !data) return <div className="text-sm text-red-500">Не смог загрузить: {fmtErr(error ?? "нет данных")}</div>;

  async function create() {
    if (!name.trim()) { toast("Укажите название (напр. «Телефон»)", "info"); return; }
    setBusy(true);
    try {
      const r = await api.createMyToken({ name: name.trim(), preset });
      setFresh({ name: r.name, token: r.token });
      toast("Токен создан ✓ — скопируйте его в плеер (показывается один раз)", "ok");
      mutate();
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string, label: string) {
    if (!confirm(`Удалить токен «${label}»? Приложения с ним перестанут работать.`)) return;
    try {
      await api.deleteMyToken(id);
      mutate();
      toast("Токен удалён", "ok");
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    }
  }

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      toast("Скопировано ✓", "ok");
    } catch {
      toast("Не смог скопировать — выделите вручную", "info");
    }
  }

  const toks = data.tokens ?? [];

  return (
    <div className="space-y-3">
      <Card>
        <div className="flex items-center gap-2 flex-wrap text-sm">
          <KeyRound className="w-4 h-4" />
          <input
            className="kuma-input kuma-input-inline w-44"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Мой телефон"
            maxLength={128}
          />
          <select className="kuma-input kuma-input-inline" value={preset} onChange={(e) => setPreset(e.target.value)}>
            {PRESETS.map((p) => (
              <option key={p.key} value={p.key}>{p.label}</option>
            ))}
          </select>
          <Button onClick={create} disabled={busy}>
            <Plus className="w-4 h-4" /> {busy ? "Создаю…" : "Выпустить токен"}
          </Button>
        </div>
        <div className="text-xs text-muted mt-2">
          Один токен — одно устройство. Токен привязан к вам и ходит только под вашим пользователем.
        </div>
      </Card>

      {fresh && (
        <Card>
          <div className="font-medium text-sm mb-1">Токен «{fresh.name}» — показывается один раз</div>
          <div className="flex items-center gap-2 flex-wrap">
            <code className="kuma-pill text-xs break-all">{fresh.token}</code>
            <Button variant="ghost" onClick={() => copy(fresh.token)}>
              <Copy className="w-3 h-3" /> Скопировать
            </Button>
            <button className="kuma-pill text-xs" onClick={() => setFresh(null)}>Скрыть</button>
          </div>
          <div className="text-xs text-muted mt-1">Вставьте его в плеер (Настройки → ML → Мозг → Токен).</div>
        </Card>
      )}

      <Card>
        <div className="font-medium text-sm mb-1">Мои токены · {toks.length}</div>
        {toks.length === 0 ? (
          <div className="text-xs text-muted">Токенов пока нет — выпустите первый выше.</div>
        ) : (
          <div className="divide-y divide-border">
            {toks.map((t) => (
              <div key={t.id} className="py-2 flex items-center gap-2 text-sm flex-wrap">
                <span className="font-medium">{t.name}</span>
                <code className="kuma-pill text-[11px]">{t.prefix}…</code>
                <Badge tone={t.enabled ? "ok" : "default"}>{t.enabled ? "вкл" : "выкл"}</Badge>
                <span className="text-[11px] text-muted">
                  {(t.scopes ?? []).join(" + ")}
                </span>
                <span className="text-[11px] text-muted">
                  {t.last_used_at ? `был: ${fmtDate(t.last_used_at)}` : "не использовался"}
                </span>
                <span className="flex-1" />
                <Button variant="ghost" onClick={() => remove(t.id, t.name)} title="Удалить">
                  <Trash2 className="w-3 h-3" />
                </Button>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
