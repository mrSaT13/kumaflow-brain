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

function Row({ k, v }: { k: string; v: unknown }) {
  return (
    <div className="flex items-center justify-between">
      <dt className="text-muted">{k}</dt>
      <dd>{v === undefined || v === null || v === "" ? "—" : String(v)}</dd>
    </div>
  );
}

function DiagRow({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <dt className="text-muted shrink-0">{k}</dt>
      <dd className="text-right break-all">{v || "—"}</dd>
    </div>
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
  const [aiProvider, setAiProvider] = useState("NONE");
  const [aiModel, setAiModel] = useState("");
  const [aiModels, setAiModels] = useState<string[]>([]);
  const [openaiKey, setOpenaiKey] = useState("");
  const [openaiUrl, setOpenaiUrl] = useState("");
  const [ollamaUrl, setOllamaUrl] = useState("");
  const [ollamaCloudKey, setOllamaCloudKey] = useState("");
  const [geminiKey, setGeminiKey] = useState("");
  const [mistralKey, setMistralKey] = useState("");
  const [aiBusy, setAiBusy] = useState(false);
  const [modelsBusy, setModelsBusy] = useState(false);
  const [aiPrompt, setAiPrompt] = useState("Скажи ok одним словом");
  const [aiResult, setAiResult] = useState<string | null>(null);
  const { data: aiCfg, mutate: mutateAi } = useSWR("/api/settings/ai", () => api.getAi());
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

  useEffect(() => {
    if (!aiCfg?.saved) return;
    const s = aiCfg.saved;
    if (s.provider) setAiProvider(s.provider);
    if (s.model) setAiModel(s.model);
    if (s.openai_api_key) setOpenaiKey(s.openai_api_key);
    if (s.openai_server_url) setOpenaiUrl(s.openai_server_url);
    if (s.ollama_server_url) setOllamaUrl(s.ollama_server_url);
    if (s.ollama_cloud_api_key) setOllamaCloudKey(s.ollama_cloud_api_key);
    if (s.gemini_api_key) setGeminiKey(s.gemini_api_key);
    if (s.mistral_api_key) setMistralKey(s.mistral_api_key);
  }, [aiCfg]);

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

  async function saveAi() {
    setAiBusy(true);
    try {
      const r = await api.saveAi({
        provider: aiProvider,
        model: aiModel.trim(),
        openai_api_key: openaiKey.trim(),
        openai_server_url: openaiUrl.trim(),
        ollama_server_url: ollamaUrl.trim(),
        ollama_cloud_api_key: ollamaCloudKey.trim(),
        gemini_api_key: geminiKey.trim(),
        mistral_api_key: mistralKey.trim(),
      });
      if (!r.ok) return alert(r.error ?? "Ошибка сохранения");
      await Promise.all([mutate(), mutateAi()]);
      alert("Сохранено ✓");
    } catch (e: unknown) {
      alert(String(e));
    } finally {
      setAiBusy(false);
    }
  }

  async function loadModels() {
    setModelsBusy(true);
    try {
      const r = await api.aiModels();
      setAiModels(r.available ?? []);
      if (!r.available?.length) alert("Список моделей пуст — проверьте ключ/URL и сохраните настройки");
    } catch (e: unknown) {
      alert(String(e));
    } finally {
      setModelsBusy(false);
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

      <Section title="AI — провайдер и модели">
        <Card>
          <div className="text-sm text-muted mb-3">
            Статус:{" "}
            <Badge tone={aiCfg?.configured ? "ok" : "warn"}>
              {aiCfg ? (aiCfg.configured ? "настроен" : "не настроен") : "…"}
            </Badge>{" "}
            {aiCfg?.effective?.provider && aiCfg.effective.provider !== "NONE" && (
              <span className="text-xs">
                · {aiCfg.effective.provider} · модель: {aiCfg.effective.model || "—"}
              </span>
            )}
            <span className="text-xs"> · используется для анализа настроения текстов и cold-start описаний</span>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <label className="block">
              <div className="text-xs text-muted mb-1">Провайдер</div>
              <select className="kuma-input" value={aiProvider} onChange={(e) => setAiProvider(e.target.value)}>
                <option value="NONE">Выключен</option>
                <option value="OLLAMA">Ollama (локальная)</option>
                <option value="OLLAMA_CLOUD">Ollama Cloud</option>
                <option value="OPENAI">OpenAI-совместимый</option>
                <option value="GEMINI">Google Gemini</option>
                <option value="MISTRAL">Mistral</option>
              </select>
            </label>
            <div className="block">
              <div className="text-xs text-muted mb-1">Модель</div>
              <div className="flex gap-2">
                {aiModels.length > 0 ? (
                  <select className="kuma-input flex-1" value={aiModel} onChange={(e) => setAiModel(e.target.value)}>
                    <option value="">— выбрать —</option>
                    {aiModels.map((m) => (
                      <option key={m} value={m}>{m}</option>
                    ))}
                  </select>
                ) : (
                  <Input className="flex-1" value={aiModel} onChange={(e) => setAiModel(e.target.value)} placeholder="llama3.1 / gpt-4o-mini / gemini-2.0-flash" />
                )}
                <Button variant="ghost" onClick={loadModels} disabled={modelsBusy}>
                  {modelsBusy ? "…" : "Модели"}
                </Button>
              </div>
            </div>
            {(aiProvider === "OPENAI") && (
              <>
                <Field label="API-ключ" value={openaiKey} onChange={setOpenaiKey} type="password" placeholder="sk-…" />
                <Field label="Server URL (опционально)" value={openaiUrl} onChange={setOpenaiUrl} placeholder="https://api.openai.com/v1/chat/completions" />
              </>
            )}
            {aiProvider === "OLLAMA" && (
              <Field label="Ollama URL" value={ollamaUrl} onChange={setOllamaUrl} placeholder="http://localhost:11434" />
            )}
            {aiProvider === "OLLAMA_CLOUD" && (
              <Field label="Ollama Cloud API key" value={ollamaCloudKey} onChange={setOllamaCloudKey} type="password" placeholder="взять на ollama.com/settings → API keys" />
            )}
            {aiProvider === "GEMINI" && (
              <Field label="Gemini API key" value={geminiKey} onChange={setGeminiKey} type="password" placeholder="AIza…" />
            )}
            {aiProvider === "MISTRAL" && (
              <Field label="Mistral API key" value={mistralKey} onChange={setMistralKey} type="password" placeholder="…" />
            )}
          </div>
          <div className="mt-4 flex gap-2 flex-wrap">
            <Button onClick={saveAi} disabled={aiBusy}>
              <Check className="w-4 h-4" /> {aiBusy ? "Сохранение…" : "Сохранить"}
            </Button>
          </div>
          <div className="text-xs text-muted mt-3">
            Сохраняется в базе — перезапуск и правки compose/env не нужны. Кнопка «Модели» подтягивает актуальный список с сервера провайдера.
          </div>
          <div className="flex gap-2 mt-3">
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
          {!data ? (
            <div className="text-sm text-muted">Загрузка… (если висит — проверьте, что backend доступен через /api)</div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
              <div>
                <div className="text-xs uppercase tracking-wider text-muted mb-2">Сервис</div>
                <dl className="space-y-1">
                  <DiagRow k="Приложение" v={String(rt["app_name"] ?? "—")} />
                  <DiagRow k="Версия" v={String(rt["version"] ?? "—")} />
                  <DiagRow k="Окружение" v={String(rt["env"] ?? "—")} />
                  <DiagRow k="Медиа-сервер (env)" v={`${String(rt["media_server_type"] ?? "—")} · ${String(rt["navidrome_url"] ?? "—")}`} />
                  <DiagRow k="AI" v={`${String(rt["ai_provider"] ?? "NONE")} ${rt["ai_configured"] ? "(настроен)" : "(не настроен)"}`} />
                  <DiagRow k="Мост" v={String(rt["bridge_enabled"]) === "true" ? String(rt["bridge_url"]) : "выключен"} />
                  <DiagRow k="БД" v={String(rt["database_url"] ?? "—")} />
                </dl>
              </div>
              <div>
                <div className="text-xs uppercase tracking-wider text-muted mb-2">Настройки в базе</div>
                <pre className="text-xs text-muted whitespace-pre-wrap max-h-60 overflow-auto">
                  {JSON.stringify(data.db ?? {}, null, 2)}
                </pre>
              </div>
            </div>
          )}
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
