"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { ExitSportsHedge } from "./exit-sports-hedge";

const navGroups = [
  {
    title: "Arbitrage",
    items: [
      { href: "/", label: "Operations console", icon: "ARB" },
      { href: "/arbitrage/priority-alerts", label: "Priority Alerts", icon: "PA" },
    ],
  },
  {
    title: "Research",
    items: [
      { href: "/research", label: "Research Home", icon: "RH" },
      { href: "/matchday", label: "Matchday", icon: "MD" },
      { href: "/teams", label: "Team Explorer", icon: "TM" },
      { href: "/scenario-lab", label: "Scenario Lab", icon: "SL" },
      { href: "/scenario-planner", label: "Scenario Planner", icon: "PL" },
      { href: "/market-intelligence", label: "Market Intelligence", icon: "MI" },
      { href: "/trends", label: "Trend Explorer", icon: "TR" },
    ],
  },
  {
    title: "Books",
    items: [
      { href: "/paper", label: "Paper Portfolio", icon: "PP" },
      { href: "/treasury", label: "Treasury", icon: "TRSY" },
    ],
  },
];

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">SH</div>
        <div>
          <div className="brand-title">SPORTS HEDGE</div>
          <div className="brand-subtitle">paper operations terminal</div>
        </div>
      </div>

      {navGroups.map((group) => (
        <div key={group.title}>
          <div className="nav-group-title">{group.title}</div>
          <nav className="nav">
            {group.items.map((item) => {
              const active =
                item.href === "/"
                  ? pathname === "/" || pathname.startsWith("/arbitrage/fixtures")
                  : item.href === "/teams"
                    ? pathname.startsWith("/teams")
                    : pathname === item.href || pathname.startsWith(`${item.href}/`);
              return (
                <Link
                  className={`nav-link ${active ? "active" : ""}`}
                  href={item.href}
                  key={item.href}
                >
                  <span className="nav-icon">{item.icon}</span>
                  <span>{item.label}</span>
                </Link>
              );
            })}
          </nav>
        </div>
      ))}

      <div className="sidebar-footer">
        <div className="paper-pill"><span className="paper-dot" /> PAPER MODE</div>
        <div className="sidebar-note">
          Research and simulation only. Live execution is disabled at the application boundary.
        </div>
        <ExitSportsHedge />
      </div>
    </aside>
  );
}
