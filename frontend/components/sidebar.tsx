"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { ExitSportsHedge } from "./exit-sports-hedge";
import { RuntimeModeFooter, useRuntimeMode } from "./runtime-mode-provider";

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

function simulationNavLabel(href: string, label: string): string {
  if (href === "/arbitrage/priority-alerts") return "Priority Alerts · simulation";
  if (href === "/paper") return "Paper Portfolio · simulation";
  if (href === "/treasury") return "Simulation Treasury";
  return label;
}

export function Sidebar() {
  const pathname = usePathname();
  const runtime = useRuntimeMode();
  const simulation = runtime.simulationSection;

  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">SH</div>
        <div>
          <div className="brand-title">SPORTS HEDGE</div>
          <div className="brand-subtitle">{runtime.subtitle}</div>
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
                  <span>{simulation ? simulationNavLabel(item.href, item.label) : item.label}</span>
                </Link>
              );
            })}
          </nav>
        </div>
      ))}

      <div className="sidebar-footer">
        <RuntimeModeFooter />
        <ExitSportsHedge />
      </div>
    </aside>
  );
}
