export function fmtDuration(s?: number) {
  if (!s || s < 0) return "—";
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${m}:${sec.toString().padStart(2, "0")}`;
}

export function fmtDate(iso?: string | null) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

export function fmtTime(iso?: string | null) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString("ru-RU", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return iso;
  }
}

export function fmtNumber(n?: number | null) {
  if (n === undefined || n === null) return "—";
  return new Intl.NumberFormat("ru-RU").format(n);
}

export const PHASE_LABELS: Record<string, string> = {
  library: "Сканирование библиотеки",
  analysis: "Sonic-анализ",
  lyrics: "Тексты и AI-настроение",
  clusters: "Кластеризация",
  collab: "Коллаборативная фильтрация",
};

export const STATUS_LABELS: Record<string, string> = {
  queued: "в очереди",
  running: "выполняется",
  success: "успех",
  failure: "ошибка",
};

export const STATUS_TONE: Record<string, "ok" | "warn" | "err" | "info" | "default"> = {
  queued: "info",
  running: "warn",
  success: "ok",
  failure: "err",
};
