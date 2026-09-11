import type { Metadata } from "next";
import "./globals.css";
import { Sidebar } from "../components/sidebar";

export const metadata: Metadata = {
  title: "Sports Hedge",
  description: "Paper-only sports market research and arbitrage dashboard",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <Sidebar />
          <div className="content">
            <header className="topbar">
              <div className="status-cluster">
                <span className="status-item"><span className="status-dot" /> Matchbook data</span>
                <span className="status-item"><span className="status-dot readonly" /> Polymarket read-only</span>
              </div>
              <div className="clock">GBP · PAPER · DEMO DATA</div>
            </header>
            <main className="main">{children}</main>
          </div>
        </div>
      </body>
    </html>
  );
}
