import type { Metadata } from "next";
import "./globals.css";
import { LiveStatusProvider } from "../components/live-status-provider";
import { RuntimeModeClock, RuntimeModeProvider } from "../components/runtime-mode-provider";
import { Sidebar } from "../components/sidebar";
import { SoundAlerts } from "../components/sound-alerts";
import { VenueHealthBar } from "../components/venue-health-bar";

export const metadata: Metadata = {
  title: "Sports Hedge",
  description: "Sports Hedge operator console",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <LiveStatusProvider>
          <RuntimeModeProvider>
            <div className="shell">
              <Sidebar />
              <div className="content">
                <header className="topbar">
                  <VenueHealthBar />
                  <SoundAlerts />
                  <RuntimeModeClock />
                </header>
                <main className="main">{children}</main>
              </div>
            </div>
          </RuntimeModeProvider>
        </LiveStatusProvider>
      </body>
    </html>
  );
}
