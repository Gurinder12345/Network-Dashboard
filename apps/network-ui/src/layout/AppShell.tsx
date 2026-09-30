import type { ReactNode } from "react";
import { FleetHealthProvider } from "../hooks/FleetHealthContext";
import { Header } from "./Header";
import { Sidebar } from "./Sidebar";

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <FleetHealthProvider>
      <div className="app-shell">
        <Header />
        <Sidebar />
        <main className="app-main">
          <div className="app-content">{children}</div>
        </main>
      </div>
    </FleetHealthProvider>
  );
}
