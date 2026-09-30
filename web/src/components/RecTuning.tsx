"use client";

import useSWR from "swr";
import { useMemo, useState } from "react";
import { RotateCcw, Save } from "lucide-react";
import { Badge, Button, Card } from "@/components/ui";
import { useToast, fmtErr } from "@/components/toasts";
import { api } from "@/lib/api";
import { useIsAdmin } from "@/lib/useIsAdmin";
import { fmtDate } from "@/lib/format";

function fmtV(v: unknown): string {
  if (Array.isArray(v)) {
    return v.map((x) => (typeof x === "number" ? String(Math.round(x * 1000) / 1000) : String(x))).join(" / ");
  }
  if (typeof v === "number") return String(Math.round(v * 1000) / 1000);
  if (v === null || v === undefined) return "—";
  return String(v);
}

/** Панель «Сейчас применено»: что авто-мозг реально делал с этим юзером.
 *  Только в Настройках (бейджа на /wave нет — так зафиксировано). */
function AppliedNow({ scope, log }: {
  scope: string;
  log: { at?: string; tuned?: boolean; skip?: string; reasons?: string[]; changes?: Record<string, Record<string, [unknown, unknown]>>; signals?: Record<string, unknown> }[] | null | undefined;
}) {
  if (!scope) return null;
  return (
    <Card>
      <div className="font-medium text-sm mb-1">Сейчас применено — выбор алгоритма в реальном времени</div>
      {!log || log.length === 0 ? (
        <div className="text-xs text-muted">
          Авто ещё ничего не меняло у этого слушателя: либо выключено, либо мало сигналов (нужно {50}+ решений за 7 дней), либо всё в норме.
        </div>
      ) : (
        <div className="space-y-2">
          {log.slice(0, 8).map((e, i) => (
            <div key={i} className="text-xs border-b border-border last:border-0 pb-2">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-muted">{e.at ? fmtDate(e.at) : "—"}</span>
                {e.tuned
                  ? <Badge tone="ok">настроено</Badge>
                  : <Badge tone="default">без изменений</Badge>}
                {e.signals && (
                  <span className="text-muted">
                    решений {String(e.signals.decided ?? "?")} · скипы {String(e.signals.early_skip_rate ?? "?")}% · дослушивания {String(e.signals.completion_rate ?? "?")}%
                  </span>
                )}
              </div>
              {e.tuned ? (
                <>
                  {(e.reasons ?? []).length > 0 && (
                    <div className="mt-1">{e.reasons!.join("; ")}</div>
                  )}
                  {e.changes && (
                    <div className="mt-1 font-mono text-[11px] space-y-0.5">
                      {Object.entries(e.changes).map(([g, sub]) => (
                        Object.entries(sub).map(([k, [b, a]]) => (
                          <div key={`${g}.${k}`}>
                            <span className="text-muted">{g}.{k}:</span> {fmtV(b)} → {fmtV(a)}
                          </div>
                        ))
                      ))}
                    </div>
                  )}
                </>
              ) : (
                e.skip && <div className="mt-1 text-muted">{e.skip}</div>
              )}
            </div>
          ))}
        </div>
      )}
      <div className="text-[11px] text-muted mt-2">
        Источник каждого значения виден по бейджу у поля: личное (оверлей юзера) / глобал (рамки) / дефолт (код).
      </div>
    </Card>
  );
}

type Kind = "int" | "float" | "ints" | "floats" | "weights";
type F = { key: string; label: string; hint?: string; kind: Kind; len?: number };

const WEIGHT_SUBS = ["audio", "genre", "artist", "behavior", "collab", "novelty"];

const GROUPS: { id: string; title: string; desc: string; fields: F[] }[] = [
  {
    id: "repeats", title: "Повторы и разнообразие",
    desc: "Как сильно волна избегает повторов артиста/жанра/настроения, усталость за неделю, новизна.",
    fields: [
      { key: "artist_penalty", label: "Штраф повтора артиста (1/2/3+)", kind: "floats", len: 3 },
      { key: "genre_penalty", label: "Штраф повтора жанра (1/2/3+)", kind: "floats", len: 3 },
      { key: "mood_penalty", label: "Штраф повтора настроения (1/2/3+)", kind: "floats", len: 3 },
      { key: "fatigue_days", label: "Окно усталости, дней", kind: "int" },
      { key: "fatigue_thresholds", label: "Пороги усталости (повторы)", kind: "ints", len: 3 },
      { key: "fatigue_penalty", label: "Штрафы усталости", kind: "floats", len: 3 },
      { key: "recency_windows_h", label: "Окна давности, часы", hint: "слышал недавно — новизна режется", kind: "ints", len: 3 },
      { key: "recency_factors", label: "Множители новизны по окнам", kind: "floats", len: 3 },
      { key: "novelty_extra", label: "Добавка новизны в скор", kind: "float" },
      { key: "artist_cap", label: "Макс треков артиста (AI/открытия)", kind: "int" },
    ],
  },
  {
    id: "character", title: "Характер волны",
    desc: "Веса холодной/тёплой волны, случайность, осторожность к скипам, мелкие бонусы.",
    fields: [
      { key: "likes_threshold", label: "Лайков до «тёплой» волны", kind: "int" },
      { key: "weights_cold", label: "Веса холодной", kind: "weights" },
      { key: "weights_warm", label: "Веса тёплой", kind: "weights" },
      { key: "jitter", label: "Случайность", kind: "float" },
      { key: "skip_weight", label: "Вес скип-риска", kind: "float" },
      { key: "clap_weight", label: "Вес CLAP", kind: "float" },
      { key: "clap_max", label: "Потолок CLAP", kind: "float" },
      { key: "key_weight", label: "Вес тональности", kind: "float" },
      { key: "cluster_weight", label: "Вес кластера", kind: "float" },
      { key: "lyrics_weight", label: "Вес лирики", kind: "float" },
      { key: "time_max", label: "Потолок «в этот час»", kind: "float" },
      { key: "arm_min", label: "Бандит мин", kind: "float" },
      { key: "arm_max", label: "Бандит макс", kind: "float" },
      { key: "assoc_weight", label: "Вес ассоциаций", kind: "float" },
      { key: "srv_starred", label: "Бонус ♥ сервера", kind: "float" },
      { key: "srv_rating", label: "Бонус оценки сервера", kind: "float" },
      { key: "mood_arm_min", label: "Муд-бандит мин", kind: "float" },
      { key: "mood_arm_max", label: "Муд-бандит макс", kind: "float" },
      { key: "neg_weight", label: "Штраф за похожее на дизлайки", kind: "float" },
    ],
  },
  {
    id: "skips", title: "Чувствительность к скипам",
    desc: "Пороги дрейфа, бан жанра, таблица behavior-бонусов.",
    fields: [
      { key: "drift_counts", label: "Скипы подряд (mild/mod/strong)", kind: "ints", len: 3 },
      { key: "drift_energy", label: "Сдвиг энергии", kind: "floats", len: 3 },
      { key: "drift_tempo", label: "Сдвиг темпа", kind: "ints", len: 3 },
      { key: "genre_ban_skips", label: "Скипов жанра до бана", kind: "int" },
      { key: "behavior_window", label: "Окно событий для бонуса", kind: "int" },
      { key: "bonus_like", label: "Бонус лайка", kind: "float" },
      { key: "bonus_replay", label: "Бонус реплея", kind: "float" },
      { key: "bonus_complete", label: "Бонус дослушивания", kind: "float" },
      { key: "bonus_play_long", label: "Бонус долгого play", kind: "float" },
      { key: "play_long_sec", label: "«Долго» — секунд", kind: "int" },
      { key: "penalty_abandon", label: "Штраф abandon", kind: "float" },
      { key: "penalty_skip_early", label: "Штраф раннего скипа", kind: "float" },
      { key: "skip_early_sec", label: "«Ранний» — секунд", kind: "int" },
      { key: "penalty_skip_late", label: "Штраф позднего скипа", kind: "float" },
      { key: "skip_late_sec", label: "«Поздний» — секунд", kind: "int" },
    ],
  },
  {
    id: "playlists", title: "Плейлисты и раскладка",
    desc: "Окна забытого, пороги ночи/спорта, пулы, пачки, плавность, сиды, радио.",
    fields: [
      { key: "forgotten_days", label: "Дней до «забытого»", kind: "int" },
      { key: "night_energy_max", label: "Ночь: энергия ниже", kind: "float" },
      { key: "sport_bpm_min", label: "Спорт: BPM выше", kind: "int" },
      { key: "sport_energy_min", label: "Спорт: энергия выше", kind: "float" },
      { key: "pool_limit", label: "Пул кандидатов", kind: "int" },
      { key: "score_window", label: "Скор-окно", kind: "int" },
      { key: "collab_extra", label: "Добор коллаборативных", kind: "int" },
      { key: "adaptive_strong", label: "Пачка при strong", kind: "int" },
      { key: "adaptive_moderate", label: "Пачка при moderate", kind: "int" },
      { key: "adaptive_mild", label: "Пачка при mild", kind: "int" },
      { key: "adaptive_morphing", label: "Пачка при морфинге", kind: "int" },
      { key: "adaptive_floor", label: "Пол пачки", kind: "int" },
      { key: "smooth_max_step", label: "Макс шаг энергии", kind: "float" },
      { key: "smooth_passes", label: "Проходов сглаживания", kind: "int" },
      { key: "key_energy_max", label: "Допуск энергии (key)", kind: "float" },
      { key: "key_passes", label: "Проходов key", kind: "int" },
      { key: "seed_limit", label: "Сидов вкуса", kind: "int" },
      { key: "radio_similar", label: "Радио: похожих артистов", kind: "int" },
      { key: "radio_per_similar", label: "Радио: треков с каждого", kind: "int" },
      { key: "radio_server", label: "Радио: серверный добор", kind: "int" },
    ],
  },
];

const PRESETS: { id: string; title: string; desc: string; set: Record<string, number | number[]> }[] = [
  {
    id: "fresh", title: "Больше новинок",
    desc: "Новизна вверх, повторы мягче, случайность выше.",
    set: {
      "repeats.novelty_extra": 0.15, "repeats.artist_cap": 1,
      "repeats.recency_factors": [0.5, 0.8, 0.95],
      "character.jitter": 0.18,
    },
  },
  {
    id: "cozy", title: "Консервативно (любимое)",
    desc: "Проверенное вверх, новизна и случайность вниз.",
    set: {
      "repeats.novelty_extra": 0.02, "character.jitter": 0.06,
      "repeats.recency_factors": [0.15, 0.4, 0.75],
    },
  },
  {
    id: "norepeat", title: "Без повторов",
    desc: "Жёсткие штрафы повторов, по одному треку артиста.",
    set: {
      "repeats.artist_penalty": [0.15, 0.25, 0.4],
      "repeats.genre_penalty": [0.1, 0.2, 0.3],
      "repeats.mood_penalty": [0.1, 0.2, 0.3],
      "repeats.artist_cap": 1,
    },
  },
];

type Num = number | number[] | Record<string, number>;

function getEff(effective: Record<string, Record<string, unknown>> | undefined, g: string, k: string): unknown {
  return effective?.[g]?.[k];
}

/** Вкладка «Волна»: per-user тюнинг. Scope «Все» = глобальные рамки,
 *  конкретный юзер = личный оверлей (трогаем только изменённое,
 *  остальное наследуется — видно по бейджу источника). */
export default function RecTuning() {
  const [scope, setScope] = useState<string>("");
  const { data: users } = useSWR("/api/users", () => api.listUsers());
  const key = `/api/settings/rec-tuning?u=${scope}`;
  const { data, mutate } = useSWR(key, () => api.getRecTuning(scope || undefined), { refreshInterval: 15000 });
  const [dirty, setDirty] = useState<Record<string, Num>>({});
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const { isAdmin } = useIsAdmin();

  const eff = data?.effective;
  const auto = (eff?.auto ?? {}) as Record<string, boolean>;

  function srcOf(g: string, k: string): "личное" | "глобал" | "дефолт" {
    if (scope) {
      const u = (data?.user_stored ?? {}) as Record<string, Record<string, unknown>>;
      if (u?.[g] && k in u[g]) return "личное";
    }
    const s = (data?.stored ?? {}) as Record<string, Record<string, unknown>>;
    if (s?.[g] && k in s[g]) return "глобал";
    return "дефолт";
  }

  function valOf(g: string, k: string): Num | undefined {
    const dk = `${g}.${k}`;
    if (dk in dirty) return dirty[dk];
    return getEff(eff, g, k) as Num | undefined;
  }

  function setScalar(g: string, k: string, raw: string, isInt: boolean) {
    const dk = `${g}.${k}`;
    if (raw.trim() === "") {
      setDirty((m) => { const n = { ...m }; delete n[dk]; return n; });
      return;
    }
    const v = isInt ? parseInt(raw, 10) : parseFloat(raw);
    if (!Number.isFinite(v)) return;
    setDirty((m) => ({ ...m, [dk]: v }));
  }

  function setListIdx(g: string, k: string, i: number, raw: string, isInt: boolean) {
    const cur = (valOf(g, k) as number[] | undefined) ?? [];
    const next = [...cur];
    if (raw.trim() === "") return;
    const v = isInt ? parseInt(raw, 10) : parseFloat(raw);
    if (!Number.isFinite(v)) return;
    next[i] = v;
    setDirty((m) => ({ ...m, [`${g}.${k}`]: next }));
  }

  function setWeight(g: string, k: string, sub: string, raw: string) {
    const cur = (valOf(g, k) as Record<string, number> | undefined) ?? {};
    if (raw.trim() === "") return;
    const v = parseFloat(raw);
    if (!Number.isFinite(v)) return;
    setDirty((m) => ({ ...m, [`${g}.${k}`]: { ...cur, [sub]: v } }));
  }

  const dirtyCount = Object.keys(dirty).length;

  async function save() {
    if (!dirtyCount) return;
    setBusy(true);
    try {
      // dirty уже содержит полные значения (списки/словари собраны из effective),
      // группируем обратно во {group: {key: value}}
      const body: Record<string, unknown> = {};
      if (scope) body.user_id = scope;
      for (const [dk, v] of Object.entries(dirty)) {
        const dot = dk.indexOf(".");
        const g = dk.slice(0, dot);
        const k = dk.slice(dot + 1);
        (body[g] ??= {}) as Record<string, unknown>;
        (body[g] as Record<string, unknown>)[k] = v;
      }
      const r = await api.saveRecTuning(body);
      if (!r.ok) { toast("Не сохранилось", "err"); return; }
      setDirty({});
      mutate();
      toast(scope ? "Личный оверлей сохранён ✓" : "Глобальные рамки сохранены ✓", "ok");
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  async function resetScope() {
    setBusy(true);
    try {
      const body: Record<string, unknown> = { reset: true };
      if (scope) body.user_id = scope;
      await api.saveRecTuning(body);
      setDirty({});
      mutate();
      toast(scope ? "Личное сброшено — наследуется глобал ✓" : "Глобал сброшен к дефолтам ✓", "ok");
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  async function toggleAuto(group: string, cur: boolean) {
    setBusy(true);
    try {
      await api.saveRecTuning({ auto: { [group]: !cur } });
      mutate();
      toast(!cur ? `Авто по «${group}» включено — ручки серые ✓` : `Авто по «${group}» выключено`, !cur ? "ok" : "info");
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  function applyPreset(p: (typeof PRESETS)[number]) {
    setDirty((m) => ({ ...m, ...p.set }));
    toast(`Пресет «${p.title}» подставлен — нажми Сохранить`, "info");
  }

  const num = (v: unknown): string => (typeof v === "number" && Number.isFinite(v) ? String(v) : "");

  const fieldInput = (g: string, f: F, grey: boolean) => {
    const dis = busy || !isAdmin || grey;
    if (f.kind === "weights") {
      const cur = (valOf(g, f.key) as Record<string, number> | undefined) ?? {};
      return (
        <div className="grid grid-cols-3 md:grid-cols-6 gap-1.5">
          {WEIGHT_SUBS.map((s) => (
            <label key={s} className="block">
              <div className="text-[10px] text-muted mb-0.5">{s}</div>
              <input
                className="kuma-input kuma-input-inline w-full text-xs"
                type="number" step="any" value={num(cur[s])}
                placeholder={num((getEff(eff, g, f.key) as Record<string, number> | undefined)?.[s])}
                disabled={dis}
                onChange={(e) => setWeight(g, f.key, s, e.target.value)}
              />
            </label>
          ))}
        </div>
      );
    }
    if (f.kind === "ints" || f.kind === "floats") {
      const cur = (valOf(g, f.key) as number[] | undefined) ?? [];
      const n = f.len ?? 3;
      const isInt = f.kind === "ints";
      return (
        <div className={`grid gap-1.5`} style={{ gridTemplateColumns: `repeat(${n}, minmax(0,1fr))` }}>
          {Array.from({ length: n }, (_, i) => (
            <input
              key={i}
              className="kuma-input kuma-input-inline w-full text-xs"
              type="number" step="any" value={num(cur[i])}
              placeholder={num((getEff(eff, g, f.key) as number[] | undefined)?.[i])}
              disabled={dis}
              onChange={(e) => setListIdx(g, f.key, i, e.target.value, isInt)}
            />
          ))}
        </div>
      );
    }
    return (
      <input
        className="kuma-input kuma-input-inline w-32 text-xs"
        type="number" step="any" value={num(valOf(g, f.key))}
        disabled={dis}
        onChange={(e) => setScalar(g, f.key, e.target.value, f.kind === "int")}
      />
    );
  };

  const usersList = useMemo(() => users?.users ?? [], [users]);

  return (
    <>
      <Card>
        <div className="flex items-center gap-2 flex-wrap text-sm">
          <span className="font-medium">Чьи настройки</span>
          <select
            className="kuma-input kuma-input-inline min-w-52"
            value={scope}
            onChange={(e) => { setScope(e.target.value); setDirty({}); }}
          >
            <option value="">Все — глобальные рамки</option>
            {usersList.map((u) => (
              <option key={u.id} value={u.id}>{u.username}</option>
            ))}
          </select>
          {scope
            ? <Badge tone="ok">личный оверлей — трогаем только изменённое</Badge>
            : <Badge tone="default">глобал — рамки для всех</Badge>}
          {dirtyCount > 0 && <Badge tone="warn">изменено: {dirtyCount}</Badge>}
          <span className="flex-1" />
          <Button variant="ghost" onClick={resetScope} disabled={busy || !isAdmin} title={scope ? "Удалить личный оверлей — будет наследовать глобал" : "Удалить глобал — вернутся дефолты кода"}>
            <RotateCcw className="w-3 h-3" /> Сбросить
          </Button>
          <Button onClick={save} disabled={busy || !isAdmin || !dirtyCount}>
            <Save className="w-3 h-3" /> {busy ? "Сохранение…" : "Сохранить"}
          </Button>
        </div>
        <div className="text-xs text-muted mt-2">
          {scope
            ? "Пустое поле = наследовать (глобал/дефолт). Бейдж у поля показывает откуда значение: личное / глобал / дефолт."
            : "Глобал — только рамки. Личное per-user правится выбором юзера выше. Авто-мозг (R3) будет писать в личный оверлей сам."}
          {!isAdmin && " Изменение доступно админу."}
        </div>
      </Card>
      <div className="h-4" />
      <Card>
        <div className="font-medium text-sm mb-1">Пресеты</div>
        <div className="flex gap-2 flex-wrap">
          {PRESETS.map((p) => (
            <Button key={p.id} variant="ghost" onClick={() => applyPreset(p)} disabled={busy || !isAdmin} title={p.desc}>
              {p.title}
            </Button>
          ))}
        </div>
        <div className="text-xs text-muted mt-1">Пресет подставляет значения в форму — применяется кнопкой «Сохранить», в scope «{scope || "глобал"}».</div>
      </Card>
      <div className="h-4" />
      <AppliedNow scope={scope} log={data?.auto_log} />
      <div className="h-4" />
      {GROUPS.map((g) => {
        // Авто — только глобальное (секция auto в rec_tuning глобальная):
        // в личном scope тумблеры прячем, ручки всегда активны.
        const autoOn = !scope && !!auto[g.id];
        return (
          <Card key={g.id}>
            <div className="flex items-center gap-2 flex-wrap mb-1">
              <span className="font-medium text-sm">{g.title}</span>
              {!scope && (
                <label className="flex items-center gap-2 cursor-pointer ml-auto text-xs">
                  <input
                    type="checkbox"
                    checked={!!auto[g.id]}
                    disabled={busy || !isAdmin}
                    onChange={() => toggleAuto(g.id, !!auto[g.id])}
                    className="w-4 h-4 accent-black dark:accent-white shrink-0"
                  />
                  <span>Авто {auto[g.id] ? <Badge tone="ok">вкл — ручки серые</Badge> : <Badge tone="default">выкл</Badge>}</span>
                </label>
              )}
            </div>
            <div className="text-xs text-muted mb-3">{g.desc}</div>
            <div className="space-y-2.5">
              {g.fields.map((f) => (
                <div key={f.key} className={`flex flex-col md:flex-row md:items-center gap-1 md:gap-3 ${autoOn ? "opacity-50" : ""}`}>
                  <div className="md:w-64 shrink-0">
                    <span className="text-xs font-medium">{f.label}</span>{" "}
                    <Badge tone={srcOf(g.id, f.key) === "личное" ? "ok" : srcOf(g.id, f.key) === "глобал" ? "warn" : "default"}>
                      {srcOf(g.id, f.key)}
                    </Badge>
                    {f.hint && <div className="text-[11px] text-muted">{f.hint}</div>}
                  </div>
                  <div className="flex-1">{fieldInput(g.id, f, autoOn)}</div>
                </div>
              ))}
            </div>
          </Card>
        );
      })}
      <div className="h-4" />
    </>
  );
}
