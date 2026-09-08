"use client";

import useSWR from "swr";
import { useEffect, useState } from "react";
import { Check, Sparkles, Wifi } from "lucide-react";
import { Badge, Button, Card, Input, PageHeader, Section } from "@/components/ui";
import { api } from "@/lib/api";

function Field({ label, value, onChange, placeholder, type = "text" }: { label: string; value: string; onChange: (v: string) => void; placeholder?: string; type?: string }) {
  return (
    <label className="block">
      <div className="text-xs text-muted mb-1">{label}</div>
      <Input type={type} value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

export default function SettingsPage() {
  const { data, mutate } = useSWR("/api/settings", () => api.settings());
  const { data: media, mutate: mutateMedia } = useSWR("/api/settings/media-server", () => api.getMediaServer());
  const { data: bridge, mutate: mutateBridge } = useSWR("/api/settings/bridge", () => api.getBridge());

  const [type, setType] = useState("navidrome");
  const [url, setUrl] = useState("");
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [aiPrompt, setAiPrompt] = useState("Скажи ok одним словом");
  const [aiResult, setAiResult] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [testBusy, setTestBusy] = useState(false);
  const [bridgeUrl, setBridgeUrl] = useState("");
  const [bridgeEnabled, setBridgeEnabled] = useState(false);
  const [bridgeResult, setBridgeResult] = useState<string | null>(null);
  const [bridgeBusy, setBridgeBusy] = useState(false);

  useEffect(() => {
    if (!media) return;
    if (media.type) setType(media.type);
    if (media.url) setUrl(media.url);
    if (media.user) setUser(media.user);
    if (media.password) setPassword(media.password);
    if (media.token) setToken(media.token);
  }, [media]);

  useEffect(() => {
    if (!bridge) return;
    if (bridge.url) setBridgeUrl(bridge.url);
    setBridgeEnabled(Boolean(bridge.enabled));
  }, [bridge]);

  async function saveMedia() {
    if (!url.trim()) return alert("Укажите URL сервера");
    if (!user.trim()) return alert("Укажите пользователя");
    setBusy(true);
    setTestResult(null);
    try {
      const r = await api.saveMediaServer({ type, url: url.trim(), user: user.trim(), password, token: token.trim() });
      if (!r.ok) return alert(r.error ?? "Ошибка сохранения");
      await Promise.all([mutate(), mutateMedia()]);
      alert("Сохранено ✓");
    } catch (e: unknown) {
      alert(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function testConnection() {
    setTestBusy(true);
    setTestResult(null);
    try {
      const r = await api.testMediaServer({ type, url: url.trim(), user: user.trim(), password, token: token.trim() });
      if (r.ok) setTestResult(`✓ Подключение успешно${r.status ? ` (HTTP ${r.status})` : ""}`);
      else setTestResult(`✗ Ошибка: ${r.error ?? "неизвестная ошибка"}${r.status ? ` (HTTP ${r.status})` : ""}`);
    } catch (e: unknown) {
      setTestResult(`✗ Ошибка: ${String(e)}`);
    } finally {
      setTestBusy(false);
    }
  }

  async function testAi() {
    setBusy(true);
    setAiResult(null);
    try {
      const r = await api.aiTest(aiPrompt);
      setAiResult(r.ok ? (r.response ?? "ok") : `Ошибка: ${r.error}`);
    } catch (e: unknown) {
      setAiResult(`Ошибка: ${String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  async function saveBridge() {
    if (bridgeEnabled && !bridgeUrl.trim()) return alert("Укажите URL моста или выключите его");
    setBridgeBusy(true);
    setBridgeResult(null);
    try {
      const r = await api.saveBridge({ url: bridgeUrl.trim(), enabled: bridgeEnabled });
      if (!r.ok) return alert(r.error ?? "Ошибка сохранения");
      await Promise.all([mutate(), mutateBridge()]);
      alert("Сохранено ✓");
    } catch (e: unknown) {
      alert(String(e));
    } finally {
      setBridgeBusy(false);
    }
  }

  async function testBridge() {
    setBridgeBusy(true);
    setBridgeResult(null);
    try {
      const r = await api.testBridge(bridgeUrl.trim() ? { url: bridgeUrl.trim() } : undefined);
      if (r.ok) {
        const b = r.body as { version?: string; providers?: Record<string, boolean> } | undefined;
        const provs = b?.providers ? Object.entries(b.providers).map(([k, v]) => `${k}: ${v ? "✓" : "—"}`).join(", ") : "";
        setBridgeResult(`✓ Мост отвечает${b?.version ? ` (v${b.version})` : ""}${provs ? ` · ${provs}` : ""}`);
      } else {
        setBridgeResult(`✗ Ошибка: ${r.error ?? "неизвестная ошибка"}`);
      }
    } catch (e: unknown) {
      setBridgeResult(`✗ Ошибка: ${String(e)}`);
    } finally {
      setBridgeBusy(false);
    }
  }

  const rt = (data?.runtime ?? {}) as Record<string, unknown>;

  return (
    <>
      <PageHeader title="Настройки" subtitle="Подключения, токены и провайдеры" />

      <Section title="Медиа-сервер">
        <Card>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <label className="block">
              <div className="text-xs text-muted mb-1">Тип сервера</div>
              <select className="kuma-input" value={type} onChange={(e) => setType(e.target.value)}>
                <option value="navidrome">Navidrome</option>
                <option value="jellyfin">Jellyfin</option>
                <option value="emby">Emby</option>
                <option value="lyrion">Lyrion</option>
                <option value="none">Нет</option>
              </select>
            </label>
            <Field label="URL сервера *" value={url} onChange={setUrl} placeholder="https://navidrome.example.com" />
            <Field label="Пользователь *" value={user} onChange={setUser} placeholder="admin" />
            <Field label="Пароль" value={password} onChange={setPassword} type="password" placeholder="••••••••" />
            <Field label="Subsonic токен (опционально)" value={token} onChange={setToken} placeholder="если используете token вместо пароля" />
          </div>
          <div className="mt-4 flex gap-2 flex-wrap">
            <Button onClick={saveMedia} disabled={busy}>
              <Check className="w-4 h-4" /> {busy ? "Сохранение…" : "Сохранить"}
            </Button>
            <Button variant="ghost" onClick={testConnection} disabled={testBusy || !url || !user}>
              <Wifi className="w-4 h-4" /> {testBusy ? "Проверяю…" : "Проверить подключение"}
            </Button>
            <span className="text-xs text-muted self-center">
              ENV: {(rt["media_server_type"] as string) ?? "—"} · {(rt["navidrome_url"] as string) ?? "—"}
            </span>
          </div>
          {testResult && (
            <div className={`mt-3 text-sm p-3 rounded-lg border ${testResult.startsWith("✓") ? "border-emerald-300 text-emerald-700 bg-emerald-50 dark:bg-emerald-950" : "border-rose-300 text-rose-700 bg-rose-50 dark:bg-rose-950"}`}>
              {testResult}
            </div>
          )}
          <div className="text-xs text-muted mt-3">
            URL можно указывать как <code className="kuma-pill">https://navidrome.example.com</code>, <code className="kuma-pill">navidrome.example.com</code> или LAN-адрес <code className="kuma-pill">http://192.168.1.10:4533</code> — префикс добавится автоматически. Для Navidrome достаточно логина и пароля; поле «токен» нужно только если вы используете готовый Subsonic-t.
            <br />
            Важно: проверка и сканирование выполняются из backend-контейнера в Docker, где <code className="kuma-pill">localhost</code> — это сам контейнер. Если Navidrome стоит на том же хосте, укажите LAN-IP машины (например <code className="kuma-pill">http://192.168.1.10:4533</code>), а не <code className="kuma-pill">localhost:4533</code>. Сохранение — только здесь, в веб-интерфейсе; править compose-файл не нужно.
          </div>
        </Card>
      </Section>

      <Section title="AI / Ollama Cloud">
        <Card>
          <div className="text-sm text-muted mb-3">
            Поставщик: <Badge tone={rt["ai_configured"] ? "ok" : "warn"}>{String(rt["ai_provider"] ?? "NONE")}</Badge>{" "}
            {rt["ai_configured"] ? "настроен" : "не настроен"} · модель: {String(rt["ollama_cloud_model"] ?? rt["openai_model_name"] ?? "—")}
          </div>
          <div className="text-xs text-muted mb-3">
            Ollama Cloud: ключ возьмите на <a href="https://ollama.com/settings" target="_blank" className="kuma-link">ollama.com/settings → API keys</a>. Поставьте <code className="kuma-pill">AI_PROVIDER=OLLAMA_CLOUD</code> и <code className="kuma-pill">OLLAMA_CLOUD_API_KEY</code> в <code>server/.env</code>, затем перезапустите бэкенд. Для локальной Ollama используйте <code>AI_PROVIDER=OLLAMA</code>.
          </div>
          <div className="flex gap-2">
            <Input className="flex-1" value={aiPrompt} onChange={(e) => setAiPrompt(e.target.value)} placeholder="Промпт для теста" />
            <Button variant="ghost" onClick={testAi} disabled={busy}>
              <Sparkles className="w-4 h-4" /> Проверить AI
            </Button>
          </div>
          {aiResult && (
            <pre className="mt-3 p-3 rounded-lg bg-surface border border-border text-sm whitespace-pre-wrap">
              {aiResult}
            </pre>
          )}
        </Card>
      </Section>

      <Section title="Диагностика">
        <Card>
          <pre className="text-xs text-muted whitespace-pre-wrap max-h-80 overflow-auto">
            {JSON.stringify(data ?? {}, null, 2)}
          </pre>
        </Card>
      </Section>

      <Section title="Мост метаданных (MusicBrainz / Last.fm)">
        <Card>
          <div className="text-sm text-muted mb-3">
            Статус:{" "}
            <Badge tone={bridge?.enabled && bridge?.url ? "ok" : "warn"}>
              {bridge?.enabled && bridge?.url ? "включён" : "выключен"}
            </Badge>{" "}
            {bridge?.url && <span className="text-xs">· {bridge.url}</span>}
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Field label="URL моста *" value={bridgeUrl} onChange={setBridgeUrl} placeholder="http://bridge:8001" />
            <label className="block">
              <div className="text-xs text-muted mb-1">Состояние</div>
              <select
                className="kuma-input"
                value={bridgeEnabled ? "on" : "off"}
                onChange={(e) => setBridgeEnabled(e.target.value === "on")}
              >
                <option value="on">Включён</option>
                <option value="off">Выключен</option>
              </select>
            </label>
          </div>
          <div className="mt-4 flex gap-2 flex-wrap">
            <Button onClick={saveBridge} disabled={bridgeBusy}>
              <Check className="w-4 h-4" /> {bridgeBusy ? "Сохранение…" : "Сохранить"}
            </Button>
            <Button variant="ghost" onClick={testBridge} disabled={bridgeBusy}>
              <Wifi className="w-4 h-4" /> {bridgeBusy ? "Проверяю…" : "Проверить мост"}
            </Button>
          </div>
          {bridgeResult && (
            <div className={`mt-3 text-sm p-3 rounded-lg border ${bridgeResult.startsWith("✓") ? "border-emerald-300 text-emerald-700 bg-emerald-50 dark:bg-emerald-950" : "border-rose-300 text-rose-700 bg-rose-50 dark:bg-rose-950"}`}>
              {bridgeResult}
            </div>
          )}
          <div className="text-xs text-muted mt-3">
            Адрес сохраняется в базе — файлы править не нужно, бэкенд подхватит его сам.
            В Docker Compose мост доступен как <code className="kuma-pill">http://bridge:8001</code>,
            локально — <code className="kuma-pill">http://localhost:8001</code>.
            Ключ Last.fm задаётся на стороне моста (<code className="kuma-pill">LASTFM_API_KEY</code>),
            без него мост работает только через MusicBrainz.
          </div>
        </Card>
      </Section>
    </>
  );
}
