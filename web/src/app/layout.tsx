import type { Metadata } from "next";
import "@/styles/globals.css";
import { Sidebar } from "@/components/Sidebar";
import { Topbar } from "@/components/Topbar";

export const metadata: Metadata = {
  title: "KumaFlow Brain",
  description: "Самохостинг ИИ для вашей музыкальной библиотеки",
  icons: { icon: "/favicon.svg" },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ru" suppressHydrationWarning>
      <body>
        {/* Применяет сохранённую тему до гидратации, чтобы не было вспышки.
            try/catch: localStorage может быть недоступен (приватный режим). */}
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var t=localStorage.getItem('theme');if(t==='dark')document.documentElement.classList.add('dark');}catch(e){}})();`,
          }}
        />
        <div suppressHydrationWarning className="min-h-screen flex">
          <Sidebar />
          <main className="flex-1 min-w-0">
            <Topbar />
            <div className="px-8 py-6 max-w-[1400px] mx-auto">{children}</div>
          </main>
        </div>
      </body>
    </html>
  );
}
