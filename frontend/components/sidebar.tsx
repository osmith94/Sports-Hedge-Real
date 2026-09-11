"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const navItems = [
  { href: "/", label: "Arbitrage", icon: "ARB" },
  { href: "/market-intelligence", label: "Market Intelligence", icon: "MI" },
  { href: "/trends", label: "Trend Explorer", icon: "TR" },
  { href: "/paper", label: "Paper Portfolio", icon: "PP" },
];

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">SH</div>
        <div>
          <div className="brand-title">SPORTS HEDGE</div>
          <div className="brand-subtitle">market research terminal</div>
        </div>
      </div>

      <div className="nav-group-title">Workspace</div>
      <nav className="nav">
        {navItems.map((item) => {
          const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
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

      <div className="sidebar-footer">
        <div className="paper-pill"><span className="paper-dot" /> PAPER MODE</div>
        <div className="sidebar-note">
          Research and simulation only. Live execution is disabled at the application boundary.
        </div>
      </div>
    </aside>
  );
}
