import type { Metadata } from "next";
import "@/styles/globals.css";
import { MobileShell } from "@/components/MobileShell";
import { ToastProvider } from "@/components/toasts";
import LoginGate from "@/components/LoginGate";

export const metadata: Metadata = {
  title: "KumaFlow Brain",
  description: "Музыкальная аналитика и рекомендации для вашей библиотеки",
  icons: { icon: "/app-icon.png", apple: "/app-icon.png" },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ru" suppressHydrationWarning>
      <body>
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var t=localStorage.getItem('theme');if(t==='dark')document.documentElement.classList.add('dark');}catch(e){}})();`,
          }}
        />
        <ToastProvider>
          <LoginGate>
            <MobileShell>{children}</MobileShell>
          </LoginGate>
        </ToastProvider>
      </body>
    </html>
  );
}
