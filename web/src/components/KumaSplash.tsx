"use client";

/** Сплэш-загрузка как у плеера (kumaflow-release/src/app/components/splash-screen.tsx).
 *  Фиксированный тёмный градиент + кольца + бесконечный прогресс.
 *  Без таймера: висит пока родителю нужен loading, снимается размонтированием. */
export default function KumaSplash({ status = "Загрузка…" }: { status?: string }) {
  return (
    <div className="min-h-screen bg-gradient-to-br from-emerald-950 via-blue-950 to-purple-950 flex items-center justify-center relative overflow-hidden">
      <div className="absolute inset-0 overflow-hidden" aria-hidden>
        <div className="absolute -top-40 -right-40 w-80 h-80 bg-emerald-500/20 rounded-full blur-3xl animate-pulse" />
        <div className="absolute -bottom-40 -left-40 w-80 h-80 bg-blue-500/20 rounded-full blur-3xl animate-pulse" />
        <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-96 h-96 bg-purple-500/10 rounded-full blur-3xl animate-pulse" />
      </div>

      <div className="relative z-10 text-center px-8">
        <div className="relative w-32 h-32 mx-auto mb-8">
          <div className="absolute inset-0 rounded-full border-2 border-emerald-400/50 kuma-splash-ping" />
          <div className="absolute inset-0 rounded-full border-2 border-blue-400/40 kuma-splash-ping-delayed" />
          <div className="absolute inset-0 rounded-full border-4 border-emerald-500/30 kuma-splash-spin" />
          <div className="absolute inset-2 rounded-full border-4 border-blue-500/40 kuma-splash-spin-rev" />
          <div className="absolute inset-4 rounded-full bg-gradient-to-br from-emerald-500 to-blue-500 animate-pulse flex items-center justify-center overflow-hidden">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src="/icon-192.png"
              alt="KumaFlow"
              className="w-14 h-14 rounded-2xl shadow-lg"
              onError={(e) => {
                (e.currentTarget as HTMLImageElement).style.display = "none";
              }}
            />
          </div>
        </div>

        <h1 className="text-4xl font-bold text-white mb-2 tracking-tight">KumaFlow</h1>
        <p className="text-emerald-300/80 text-sm mb-8 font-medium">
          Музыкальная аналитика и рекомендации
        </p>

        <div className="w-64 h-1.5 bg-white/10 rounded-full mx-auto overflow-hidden">
          <div className="h-full w-2/5 bg-gradient-to-r from-emerald-400 via-blue-400 to-purple-400 rounded-full kuma-splash-bar" />
        </div>

        <p className="text-white/50 mt-4 text-xs font-medium tracking-wide uppercase">{status}</p>
      </div>
    </div>
  );
}
