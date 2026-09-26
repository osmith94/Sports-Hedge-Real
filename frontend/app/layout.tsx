import type { Metadata } from "next";
import "./globals.css";
import { LiveStatusProvider } from "../components/live-status-provider";
import { Sidebar } from "../components/sidebar";
import { VenueHealthBar } from "../components/venue-health-bar";

export const metadata: Metadata = {
  title: "Sports Hedge",
  description: "Paper-only sports market research and arbitrage dashboard",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <LiveStatusProvider>
          <div className="shell">
            <Sidebar />
            <div className="content">
              <header className="topbar">
                <VenueHealthBar />
                <div className="clock">GBP · PAPER MODE · NO EXECUTION</div>
              </header>
              <main className="main">{children}</main>
            </div>
          </div>
        </LiveStatusProvider>
      </body>
    </html>
  );
}
