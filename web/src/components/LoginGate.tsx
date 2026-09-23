"use client";

import { useState } from "react";
import useSWR from "swr";
import { KeyRound, LogIn, UserRound } from "lucide-react";
import { Button, Card, Input } from "@/components/ui";
import { useToast, fmtErr } from "@/components/toasts";
import { api, setBrowserToken } from "@/lib/api";

/** Gate: если API закрыт токенами, а у браузера токена нет — окно логина. */
export default function LoginGate({ children }: { children: React.ReactNode }) {
  const { data, error, isLoading, mutate } = useSWR("/api/settings/whoami", () => api.whoami(), { refreshInterval: 60000 });
  const [mode, setMode] = useState<"navidrome" | "token">("navidrome");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const toast = useToast();

  async function doLogin() {
    setBusy(true);
    setErr(null);
    try {
      const r = await api.login({ username: username.trim(), password, device: "web" });
      if (!r.ok || !r.token) {
        setErr(r.error ?? "Не получилось войти");
        return;
      }
      setBrowserToken(r.token);
      toast(`Привет, ${r.user?.username ?? username}! Токен сохранён в этот браузер.`, "ok");
      mutate();
    } catch (e: unknown) {
      setErr(fmtErr(e));
    } finally {
      setBusy(false);
    }
  }

  async function useToken() {
    if (!token.trim()) {
      setErr("Вставьте токен");
      return;
    }
    setBrowserToken(token.trim());
    setToken("");
    setErr(null);
    toast("Токен сохранён, проверяю…", "info");
    mutate();
  }

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-sm text-muted">Загрузка…</div>
      </div>
    );
  }
  // бэкенд недоступен — не лочим весь UI, страницы сами покажут ошибки
  if (error || !data) return <>{children}</>;
  // открыто по LAN — gate не нужен
  if (!data.locked || data.logged_in) return <>{children}</>;

  return (
    <div className="min-h-screen flex items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <div className="flex items-center gap-2 mb-1">
          <KeyRound className="w-5 h-5" />
          <span className="font-semibold text-lg">KumaFlow Brain</span>
        </div>
        <div className="text-xs text-muted mb-4">
          API закрыт токенами. Войдите логином Navidrome — браузер получит свой токен сам.
        </div>
        <div className="flex gap-1 mb-4 border-b border-border">
          {(["navidrome", "token"] as const).map((m) => (
            <button
              key={m}
              onClick={() => { setMode(m); setErr(null); }}
              className={`px-3 py-2 text-sm border-b-2 -mb-px ${mode === m ? "border-accent text-text" : "border-transparent text-muted hover:text-text"}`}
            >
              {m === "navidrome" ? "Логин Navidrome" : "У меня есть токен"}
            </button>
          ))}
        </div>
        {mode === "navidrome" ? (
          <div className="space-y-3">
            <label className="block">
              <div className="text-xs text-muted mb-1">Логин в Navidrome</div>
              <Input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="admin" autoComplete="username" />
            </label>
            <label className="block">
              <div className="text-xs text-muted mb-1">Пароль</div>
              <Input
                value={password} onChange={(e) => setPassword(e.target.value)} placeholder="••••••••"
                type="password" autoComplete="current-password"
                onKeyDown={(e) => { if (e.key === "Enter") void doLogin(); }}
              />
            </label>
            {err && <div className="text-xs text-red-500">{err}</div>}
            <Button onClick={doLogin} disabled={busy || !username.trim() || !password} className="w-full">
              <LogIn className="w-4 h-4" /> {busy ? "Проверяю…" : "Войти"}
            </Button>
            <div className="text-[11px] text-muted">
              Пароль используется один раз для проверки и не хранится. Админы Navidrome получают полный доступ, остальные — свой.
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            <label className="block">
              <div className="text-xs text-muted mb-1">API-токен</div>
              <Input
                value={token} onChange={(e) => setToken(e.target.value)} placeholder="вставьте токен"
                type="password"
                onKeyDown={(e) => { if (e.key === "Enter") void useToken(); }}
              />
            </label>
            {err && <div className="text-xs text-red-500">{err}</div>}
            <Button onClick={useToken} className="w-full">
              <UserRound className="w-4 h-4" /> Использовать токен
            </Button>
            <div className="text-[11px] text-muted">
              Токен создаётся в Настройки → Токены (нужен вход админа) или в плеере после логина.
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}
