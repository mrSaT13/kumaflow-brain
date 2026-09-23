import type { Metadata, Viewport } from "next";
import "@/styles/globals.css";
import { MobileShell } from "@/components/MobileShell";
import { ToastProvider } from "@/components/toasts";
import LoginGate from "@/components/LoginGate";
import PwaRegister from "@/components/PwaRegister";

export const metadata: Metadata = {
  title: "KumaFlow Brain",
  description: "Музыкальная аналитика и рекомендации для вашей библиотеки",
  manifest: "/manifest.webmanifest",
  icons: {
    icon: [
      { url: "/icon-192.png", sizes: "192x192", type: "image/png" },
      { url: "/icon-512.png", sizes: "512x512", type: "image/png" },
    ],
    apple: [{ url: "/apple-touch-icon.png", sizes: "180x180", type: "image/png" }],
  },
  appleWebApp: { capable: true, statusBarStyle: "default", title: "KumaFlow" },
};

export const viewport: Viewport = {
  themeColor: "#111111",
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
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
            <PwaRegister />
            <MobileShell>{children}</MobileShell>
          </LoginGate>
        </ToastProvider>
      </body>
    </html>
  );
}
