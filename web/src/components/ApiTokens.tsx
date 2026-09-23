"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import { Check, Copy, KeyRound, Plus, ShieldCheck, Trash2 } from "lucide-react";
import { Badge, Button, Card } from "@/components/ui";
import { useToast, fmtErr } from "@/components/toasts";
import { api, getBrowserToken, setBrowserToken, type ApiToken } from "@/lib/api";
import { fmtDate } from "@/lib/format";

/** Вкладка «Токены»: per-user API-токены плеер <-> мозг, без compose. */
export default function ApiTokens() {
  const { data: meta } = useSWR("/api/settings/tokens/meta", () => api.tokensMeta());
  const { data: users } = useSWR("/api/users", () => api.listUsers());
  const [userFilter, setUserFilter] = useState("");
  const { data, mutate } = useSWR(
    ["/api/settings/tokens", userFilter],
    () => api.listTokens(userFilter || undefined),
  );

  const [name, setName] = useState("Мобила");
  const [preset, setPreset] = useState("mobile");
  const [scopes, setScopes] = useState<string[]>(["wave", "sync", "covers", "playlists"]);
  const [busy, setBusy] = useState(false);
  const [fresh, setFresh] = useState<{ name: string; token: string } | null>(null);
  const [browserToken, setBrowserTokenState] = useState(() => getBrowserToken());
  const toast = useToast();

  const userList = useMemo(() => users?.users ?? [], [users]);
  const tokens: ApiToken[] = data?.tokens ?? [];
  const scopeLabels = meta?.scopes ?? {};
  const presets = meta?.presets ?? {};

  function pickPreset(key: string) {
    setPreset(key);
    const p = presets[key];
    if (p) setScopes([...p.scopes]);
  }

  function toggleScope(s: string) {
    setScopes((prev) => (prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s]));
  }

  async function create() {
    if (!name.trim()) { toast("Укажите название (напр. «Телефон»)", "info"); return; }
    if (scopes.length === 0) { toast("Выберите хотя бы один доступ", "info"); return; }
    setBusy(true);
    try {
      const r = await api.createToken({
        owner_user_id: userFilter || null,
        name: name.trim(),
        scopes,
      });
      setFresh({ name: r.name, token: r.token });
      mutate();
      toast("Токен создан — скопируйте, больше он не покажется", "ok");
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  async function toggle(t: ApiToken) {
    try {
      await api.toggleToken(t.id, !t.enabled);
      mutate();
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    }
  }

  async function remove(t: ApiToken) {
    if (!confirm(`Удалить токен «${t.name}» (${t.prefix}…)? Приложения с ним перестанут работать.`)) return;
    try {
      await api.deleteToken(t.id);
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

  function saveBrowser(v: string) {
    setBrowserToken(v.trim());
    setBrowserTokenState(v.trim());
    toast(v.trim() ? "Токен браузера сохранён ✓" : "Токен браузера убран", v.trim() ? "ok" : "info");
  }

  const userName = (id?: string | null) =>
    !id ? "—" : (userList.find((u) => u.id === id)?.username ?? id.slice(0, 8));

  return (
    <>
      <Card>
        <div className="flex items-center gap-2 flex-wrap text-sm">
          <KeyRound className="w-4 h-4" />
          <span className="font-medium">Доступ плеера к мозгу</span>
          {meta?.env_token_configured && <Badge tone="warn">env-токен задан (legacy, полный доступ)</Badge>}
          <Badge tone={(meta?.count ?? 0) > 0 ? "ok" : "default"}>
            {(meta?.count ?? 0) > 0 ? `токенов: ${meta?.count}` : "без токенов — открытая LAN"}
          </Badge>
        </div>
        <div className="text-xs text-muted mt-2">
          Пока токенов нет — всё работает как раньше по локальной сети. Создайте токен для каждого устройства:
          вставьте его в плеер (Настройки → ML → Мозг → Токен). Токен при потере просто удалите и создайте новый —
          в compose лезть не нужно.
        </div>
        <div className="flex items-center gap-2 mt-3 flex-wrap">
          <input
            className="kuma-input kuma-input-inline flex-1 min-w-52 text-xs"
            value={browserToken}
            onChange={(e) => setBrowserTokenState(e.target.value)}
            placeholder="Токен этого браузера (для доступа к API из веба)"
            type="password"
          />
          <Button variant="ghost" onClick={() => saveBrowser(browserToken)} disabled={busy}>
            <Check className="w-3 h-3" /> Использовать
          </Button>
          {browserToken && (
            <Button variant="ghost" onClick={() => saveBrowser("")}>Убрать</Button>
          )}
        </div>
      </Card>

      {fresh && (
        <div className="h-4" />
      )}
      {fresh && (
        <Card>
          <div className="flex items-center gap-2 text-sm font-medium">
            <ShieldCheck className="w-4 h-4 text-emerald-600" />
            Токен «{fresh.name}» — показывается один раз
          </div>
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            <code className="kuma-pill break-all text-xs flex-1">{fresh.token}</code>
            <Button variant="ghost" onClick={() => copy(fresh.token)}>
              <Copy className="w-3 h-3" /> Копировать
            </Button>
            <Button variant="ghost" onClick={() => saveBrowser(fresh.token)}>
              <Check className="w-3 h-3" /> В этот браузер
            </Button>
            <Button variant="ghost" onClick={() => setFresh(null)}>Скрыть</Button>
          </div>
        </Card>
      )}

      <div className="h-4" />
      <Card>
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-medium text-sm">Новый токен</span>
          <select
            className="kuma-input kuma-input-inline w-52 text-xs"
            value={userFilter}
            onChange={(e) => setUserFilter(e.target.value)}
            title="Владелец токена"
          >
            <option value="">Пользователь: не привязан</option>
            {userList.map((u) => (
              <option key={u.id} value={u.id}>{u.username}</option>
            ))}
          </select>
          <input
            className="kuma-input kuma-input-inline w-40 text-xs"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Название (Телефон)"
          />
        </div>
        <div className="flex items-center gap-2 mt-2 flex-wrap">
          {Object.entries(presets).map(([k, p]) => (
            <button
              key={k}
              onClick={() => pickPreset(k)}
              className={`kuma-pill text-xs ${preset === k ? "!bg-accent !text-white" : ""}`}
              title={p.desc}
            >
              {p.label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2 mt-2 flex-wrap">
          {Object.entries(scopeLabels).map(([s, label]) => (
            <label key={s} className="flex items-center gap-1.5 text-xs cursor-pointer">
              <input
                type="checkbox"
                checked={scopes.includes(s)}
                onChange={() => toggleScope(s)}
                className="w-3.5 h-3.5 accent-black dark:accent-white"
              />
              {label}
            </label>
          ))}
        </div>
        <div className="mt-3">
          <Button onClick={create} disabled={busy}>
            <Plus className="w-4 h-4" /> {busy ? "Создаю…" : "Создать токен"}
          </Button>
        </div>
      </Card>

      <div className="h-4" />
      <Card>
        <div className="flex items-center gap-2 mb-2 flex-wrap">
          <span className="font-medium text-sm">Токены {userFilter ? `· ${userName(userFilter)}` : "· все"}</span>
          {userFilter && (
            <button className="kuma-pill text-xs" onClick={() => setUserFilter("")}>Показать все</button>
          )}
        </div>
        {tokens.length === 0 ? (
          <div className="text-sm text-muted">Токенов нет — создайте первый выше.</div>
        ) : (
          <div className="divide-y divide-border">
            {tokens.map((t) => (
              <div key={t.id} className="py-3 flex flex-col md:flex-row md:items-center gap-2">
                <label className="flex items-center gap-3 min-w-0 flex-1">
                  <input
                    type="checkbox"
                    checked={t.enabled}
                    onChange={() => toggle(t)}
                    className="w-4 h-4 accent-black dark:accent-white shrink-0"
                    title={t.enabled ? "Выключить" : "Включить"}
                  />
                  <span className="min-w-0">
                    <span className="font-medium text-sm flex items-center gap-2 flex-wrap">
                      {t.name}
                      <code className="kuma-pill text-[11px]">{t.prefix}…</code>
                      <Badge tone={t.enabled ? "ok" : "default"}>{t.enabled ? "вкл" : "выкл"}</Badge>
                    </span>
                    <span className="text-xs text-muted block">
                      {t.scopes.map((s) => scopeLabels[s] ?? s).join(" · ") || "—"}
                    </span>
                    <span className="text-[11px] text-muted block">
                      {t.owner_user_id ? `владелец: ${userName(t.owner_user_id)} · ` : ""}
                      создан: {t.created_at ? fmtDate(t.created_at) : "—"}
                      {t.last_used_at ? ` · был: ${fmtDate(t.last_used_at)}` : " · не использовался"}
                    </span>
                  </span>
                </label>
                <div className="flex items-center gap-2 shrink-0">
                  <Button variant="ghost" onClick={() => remove(t)} title="Удалить токен">
                    <Trash2 className="w-3 h-3" />
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
        <div className="text-xs text-muted mt-3">
          Токен без владельца — только скоупы. Токен с владельцем ходит только под своим user_id
          (чужие вкусы/волну не достанет). Выключение мгновенно режет доступ без удаления.
        </div>
      </Card>
    </>
  );
}
