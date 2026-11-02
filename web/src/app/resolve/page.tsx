"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import { Badge, Button, Card, EmptyState, Input, PageHeader, Section, Stat } from "@/components/ui";
import { fmtErr, useToast } from "@/components/toasts";
import { api } from "@/lib/api";
import { fmtDate } from "@/lib/format";

const SOURCE_LABELS: Record<string, string> = {
  auto: "догадался сам",
  behavior: "вывел из твоих действий",
  confirmed: "ты подтвердил",
};

const SOURCE_TONE: Record<string, "ok" | "warn" | "info" | "default"> = {
  auto: "info",
  behavior: "warn",
  confirmed: "ok",
};

export default function ResolvePage() {
  const toast = useToast();
  const { data: aliasData, mutate: reloadAliases } = useSWR(
    "/api/resolve/aliases",
    () => api.resolveAliases(),
  );
  const { data: missData, mutate: reloadMisses } = useSWR(
    "/api/resolve/misses",
    () => api.resolveMisses(),
  );

  const aliases = aliasData?.aliases ?? [];
  const misses = missData?.misses ?? [];

  const [word, setWord] = useState("");
  const [target, setTarget] = useState("");
  const [bulk, setBulk] = useState("");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<{ alias: string; target: string } | null>(null);
  const [probe, setProbe] = useState("");
  const [probeResult, setProbeResult] = useState<Awaited<ReturnType<typeof api.resolveQuery>> | null>(null);

  const selfLearned = useMemo(
    () => aliases.filter((a) => a.source === "auto" || a.source === "behavior").length,
    [aliases],
  );

  async function saveAlias(q: string, name: string) {
    if (!q.trim() || !name.trim()) return;
    setBusy(true);
    try {
      await api.resolveConfirm(q.trim(), name.trim());
      toast(`Запомнил: «${q.trim()}» → ${name.trim()}`, "ok");
      setWord("");
      setTarget("");
      setEditing(null);
      await reloadAliases();
      await reloadMisses();
    } catch (e) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  async function removeAlias(alias: string) {
    setBusy(true);
    try {
      await api.resolveDelete(alias);
      toast(`Забыл: «${alias}»`, "info");
      setEditing(null);
      await reloadAliases();
    } catch (e) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  async function runImport() {
    if (!bulk.trim()) return;
    setBusy(true);
    try {
      const res = await api.resolveImport(bulk);
      toast(`Добавлено соответствий: ${res.count}`, "ok");
      setBulk("");
      await reloadAliases();
    } catch (e) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  function exportText() {
    const text = aliases.map((a) => `${a.alias} = ${a.target}`).join("\n");
    setBulk(text);
    toast(`Собрано строк: ${aliases.length}. Можно скачать или отредактировать.`, "info");
  }

  function downloadText() {
    const text = aliases.map((a) => `${a.alias} = ${a.target}`).join("\n");
    const blob = new Blob([text ? text + "\n" : ""], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "kumaflow-aliases.txt";
    a.click();
    URL.revokeObjectURL(url);
  }

  async function runProbe() {
    if (!probe.trim()) return;
    setBusy(true);
    try {
      setProbeResult(await api.resolveQuery(probe.trim()));
    } catch (e) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Умный поиск"
        subtitle="Слова, которые прилетели с голоса, и то, чему мозг научился их понимать"
        actions={
          <Button variant="ghost" onClick={() => { reloadAliases(); reloadMisses(); }}>
            Обновить
          </Button>
        }
      />

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-8">
        <Stat label="Выучено слов" value={aliases.length} hint="мозг знает, что ты имеешь в виду" />
        <Stat label="Из них догадался сам" value={selfLearned} hint="без твоих подтверждений" />
        <Stat label="Не понято пока" value={misses.length} hint="ждут, когда мозг разберётся" />
      </div>

      <Section title="Что мозг выучил">
        {aliases.length === 0 ? (
          <EmptyState message="Пока пусто. Скажи что-нибудь голосом — не найдётся, и мозг начнёт запоминать." />
        ) : (
          <div className="kuma-card overflow-hidden">
            <table className="kuma-table w-full">
              <thead>
                <tr>
                  <th>Слово</th>
                  <th>Как правильно</th>
                  <th>Откуда</th>
                  <th>Уверенность</th>
                  <th>Когда</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {aliases.map((a) => {
                  const isEditing = editing?.alias === a.alias;
                  return (
                    <tr key={`${a.alias}-${a.kind}`}>
                      <td className="font-medium">{a.alias}</td>
                      <td>
                        {isEditing ? (
                          <Input
                            defaultValue={editing.target}
                            autoFocus
                            onKeyDown={(e) => {
                              if (e.key === "Enter") {
                                const value = (e.target as HTMLInputElement).value;
                                saveAlias(editing.alias, value);
                              }
                            }}
                          />
                        ) : (
                          a.target
                        )}
                      </td>
                      <td>
                        <Badge tone={SOURCE_TONE[a.source] ?? "default"}>
                          {SOURCE_LABELS[a.source] ?? a.source}
                        </Badge>
                      </td>
                      <td className="tabular-nums text-muted">{Math.round(a.score * 100)}%</td>
                      <td className="text-xs text-muted">{fmtDate(a.created_at)}</td>
                      <td className="text-right whitespace-nowrap">
                        {isEditing ? (
                          <>
                            <button
                              className="text-sm underline mr-3"
                              onClick={() => saveAlias(editing.alias, editing.target)}
                              disabled={busy}
                            >
                              Сохранить
                            </button>
                            <button className="text-sm text-muted underline" onClick={() => setEditing(null)}>
                              Отмена
                            </button>
                          </>
                        ) : (
                          <>
                            <button
                              className="text-sm underline mr-3"
                              onClick={() => setEditing({ alias: a.alias, target: a.target })}
                            >
                              Править
                            </button>
                            <button
                              className="text-sm text-rose-600 underline"
                              onClick={() => removeAlias(a.alias)}
                              disabled={busy}
                            >
                              Забыть
                            </button>
                          </>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Section title="Что прилетало и не нашлось">
        {misses.length === 0 ? (
          <EmptyState message="Неразобранных слов нет." />
        ) : (
          <div className="kuma-card overflow-hidden">
            <table className="kuma-table w-full">
              <thead>
                <tr>
                  <th>Слово</th>
                  <th>Откуда</th>
                  <th>Когда</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {misses.map((m, i) => (
                  <tr key={`${m.query}-${i}`}>
                    <td className="font-medium">{m.query}</td>
                    <td className="text-xs text-muted">{m.source === "voice" ? "голос" : m.source}</td>
                    <td className="text-xs text-muted">{fmtDate(m.created_at)}</td>
                    <td className="text-right">
                      <button
                        className="text-sm underline"
                        onClick={() => {
                          setWord(m.query);
                          setTarget("");
                          window.scrollTo({ top: 0, behavior: "smooth" });
                        }}
                      >
                        Научить
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Section title="Научить слову">
        <Card>
          <div className="grid grid-cols-1 md:grid-cols-[1fr_1fr_auto] gap-3 items-end">
            <label className="block">
              <span className="text-xs uppercase tracking-wider text-muted">Как говоришь</span>
              <Input
                value={word}
                placeholder="эминем"
                onChange={(e) => setWord(e.target.value)}
                className="mt-1"
              />
            </label>
            <label className="block">
              <span className="text-xs uppercase tracking-wider text-muted">Как на самом деле</span>
              <Input
                value={target}
                placeholder="Eminem"
                onChange={(e) => setTarget(e.target.value)}
                className="mt-1"
              />
            </label>
            <Button onClick={() => saveAlias(word, target)} disabled={busy || !word.trim() || !target.trim()}>
              Запомнить
            </Button>
          </div>
        </Card>
      </Section>

      <Section title="Проверить слово">
        <Card>
          <div className="grid grid-cols-1 md:grid-cols-[1fr_auto] gap-3 items-end">
            <label className="block">
              <span className="text-xs uppercase tracking-wider text-muted">Что услышала колонка</span>
              <Input
                value={probe}
                placeholder="эминем"
                onChange={(e) => setProbe(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") runProbe();
                }}
                className="mt-1"
              />
            </label>
            <Button onClick={runProbe} disabled={busy || !probe.trim()}>
              Что поймёт мозг
            </Button>
          </div>
          {probeResult && (
            <div className="mt-4 text-sm">
              {probeResult.best ? (
                <>
                  <div>
                    Понял как: <span className="font-semibold">{probeResult.best.name}</span>{" "}
                    <Badge tone={probeResult.confident ? "ok" : "warn"}>
                      {probeResult.confident ? "уверенно" : "неуверенно"}
                    </Badge>{" "}
                    <span className="text-muted">({probeResult.best.why})</span>
                  </div>
                  {probeResult.candidates.length > 1 && (
                    <ul className="mt-2 text-muted space-y-1">
                      {probeResult.candidates.slice(1, 5).map((c, i) => (
                        <li key={`${c.name}-${i}`}>
                          {c.name} — {c.why} ({Math.round(c.score * 100)}%)
                        </li>
                      ))}
                    </ul>
                  )}
                </>
              ) : (
                <div className="text-muted">Ничего не нашёл — слово уйдёт в «не понял».</div>
              )}
            </div>
          )}
        </Card>
      </Section>

      <Section
        title="Массово: файлом или текстом"
        action={
          <div className="flex gap-2">
            <Button variant="ghost" onClick={exportText}>
              Собрать из выученного
            </Button>
            <Button variant="ghost" onClick={downloadText}>
              Скачать .txt
            </Button>
          </div>
        }
      >
        <Card>
          <p className="text-xs text-muted mb-2">
            По одной паре на строку: <code className="text-muted">слово = как правильно</code>.
            Поддерживаются разделители <code className="text-muted">=</code>, <code className="text-muted">-&gt;</code>,{" "}
            <code className="text-muted">→</code>, запятая и таб. Строки с <code className="text-muted">#</code> — мимо.
          </p>
          <textarea
            value={bulk}
            onChange={(e) => setBulk(e.target.value)}
            placeholder={"эминем = Eminem\nпиано гайс = The Piano Guys"}
            className="kuma-input h-40 w-full font-mono text-sm"
          />
          <div className="mt-3">
            <Button onClick={runImport} disabled={busy || !bulk.trim()}>
              Импортировать
            </Button>
          </div>
        </Card>
      </Section>
    </>
  );
}
