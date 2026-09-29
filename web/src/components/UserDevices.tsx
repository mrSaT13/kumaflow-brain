"use client";

import { useState } from "react";
import useSWR from "swr";
import { MonitorSmartphone, Pencil, RotateCcw } from "lucide-react";
import { Badge, Button, Card } from "@/components/ui";
import { useToast, fmtErr } from "@/components/toasts";
import { api } from "@/lib/api";
import { fmtDate } from "@/lib/format";

function ageText(age: number | null): string {
  if (age == null) return "молчит";
  if (age < 60) return `${age}с назад`;
  const m = Math.floor(age / 60);
  if (m < 60) return `${m} мин назад`;
  return `${Math.floor(m / 60)} ч назад`;
}

/** Вкладка «Устройства» в СВОЁМ профиле: живые слоты + переименование +
 *  токены с подсветкой дублей. Чужим не видно: бэк отдаёт только устройства
 *  владельца токена, а секция ниже рендерится лишь при isMine. */
export default function UserDevices() {
  const { data, error, isLoading, mutate } = useSWR("/api/me/devices", () => api.myDevices(), {
    refreshInterval: 15000,
  });
  const [editing, setEditing] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  if (isLoading) return <div className="text-sm text-muted">Загрузка…</div>;
  if (error || !data) {
    const msg = fmtErr(error ?? "нет данных");
    // Токен без владельца (env-админ / открытая LAN): устройств нет — есть все пользователи сразу.
    if (/403/.test(msg)) {
      return (
        <div className="text-sm text-muted">
          Войдите пользовательским токеном (не env-админ), чтобы увидеть свои устройства.
          Устройства привязаны к владельцу токена и никому другому не видны.
        </div>
      );
    }
    return <div className="text-sm text-red-500">Не смог загрузить: {msg}</div>;
  }

  async function save(device_id: string) {
    const v = name.trim();
    if (!v) {
      toast("Введите имя (напр. «Десктоп ПК») или сбросьте", "info");
      return;
    }
    setBusy(true);
    try {
      await api.renameDevice(device_id, v);
      toast(`Теперь это «${v}» — так будет в селекторе волны и в «Продолжить с …»`, "ok");
      setEditing(null);
      mutate();
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  async function reset(device_id: string) {
    setBusy(true);
    try {
      await api.renameDevice(device_id, "");
      toast("Имя сброшено к метке плеера", "ok");
      setEditing(null);
      mutate();
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  const devs = data.devices ?? [];
  const toks = data.tokens ?? [];

  return (
    <div className="space-y-3">
      {devs.length === 0 && (
        <div className="text-sm text-muted">
          Плееры ещё не отметились: откройте волну на телефоне/ПК — слот появится здесь сам.
        </div>
      )}
      {devs.map((d) => (
        <Card key={d.device_id}>
          <div className="flex items-center gap-2 flex-wrap">
            <MonitorSmartphone className="w-4 h-4 text-muted shrink-0" />
            <span className="font-medium text-sm">{d.display_name}</span>
            {d.renamed && <Badge tone="ok">своё имя</Badge>}
            <Badge tone={d.age_sec != null ? "ok" : "default"}>
              {d.age_sec != null ? `онлайн · ${ageText(d.age_sec)}` : "молчит"}
            </Badge>
            {d.paused && <Badge tone="default">пауза</Badge>}
            {d.queue_len > 0 && (
              <span className="text-xs text-muted tabular-nums">очередь: {d.queue_len}</span>
            )}
            <span className="flex-1" />
            {editing === d.device_id ? (
              <span className="flex items-center gap-1.5 flex-wrap">
                <input
                  className="kuma-input kuma-input-inline w-44 text-xs"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Десктоп ПК"
                  maxLength={64}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") void save(d.device_id);
                  }}
                />
                <Button variant="ghost" onClick={() => save(d.device_id)} disabled={busy}>
                  Ок
                </Button>
                <Button variant="ghost" onClick={() => reset(d.device_id)} disabled={busy} title="Сбросить к метке плеера">
                  <RotateCcw className="w-3 h-3" />
                </Button>
                <button className="kuma-pill text-xs" onClick={() => setEditing(null)}>✕</button>
              </span>
            ) : (
              <Button
                variant="ghost"
                onClick={() => {
                  setEditing(d.device_id);
                  setName(d.renamed ? d.display_name : "");
                }}
                title="Переименовать"
              >
                <Pencil className="w-3 h-3" /> Переименовать
              </Button>
            )}
          </div>
          {/* Что играет сейчас — сверить перед переименованием. */}
          {d.now_playing ? (
            <div className="text-xs text-muted mt-1.5">
              ▶ {d.now_playing.artist_name ? `${d.now_playing.artist_name} — ` : ""}{d.now_playing.title ?? "—"}
              {d.now_playing.position_sec != null && d.now_playing.duration_sec
                ? ` · с ${Math.floor(d.now_playing.position_sec / 60)}:${String(d.now_playing.position_sec % 60).padStart(2, "0")}`
                : ""}
            </div>
          ) : (
            d.age_sec != null && (
              <div className="text-xs text-muted mt-1.5">Сейчас ничего не играет на этом устройстве.</div>
            )
          )}
          <div className="text-[11px] text-muted mt-1 font-mono break-all" title="Сырой id слота">
            {d.raw_label !== d.display_name ? d.raw_label : d.device_id}
          </div>
        </Card>
      ))}

      <Card>
        <div className="font-medium text-sm mb-1">Токены этого пользователя</div>
        {toks.length === 0 ? (
          <div className="text-xs text-muted">Токенов нет — создайте в Настройки → Токены.</div>
        ) : (
          <div className="divide-y divide-border">
            {toks.map((t) => (
              <div key={t.id} className="py-2 flex items-center gap-2 text-sm flex-wrap">
                <span className="font-medium">{t.name}</span>
                <code className="kuma-pill text-[11px]">{t.prefix}…</code>
                <Badge tone={t.enabled ? "ok" : "default"}>{t.enabled ? "вкл" : "выкл"}</Badge>
                {t.name_duplicate && (
                  <span title="То же имя у двух токенов — введите один токен дважды в разные плееры? Каждому устройству нужен свой">
                    <Badge tone="warn">имя повторяется!</Badge>
                  </span>
                )}
                <span className="text-[11px] text-muted">
                  {t.last_used_at ? `был: ${fmtDate(t.last_used_at)}` : "не использовался"}
                </span>
              </div>
            ))}
          </div>
        )}
        <div className="text-xs text-muted mt-2">
          Один токен — одно устройство. Подсветка «имя повторяется» означает, что один и тот же
          токен, похоже, введён дважды — создайте отдельный в Настройки → Токены.
        </div>
      </Card>
    </div>
  );
}
