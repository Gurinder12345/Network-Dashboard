import type { ReactNode } from "react";
import { Header } from "./Header";
import { Sidebar } from "./Sidebar";

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="app-shell">
      <Header />
      <Sidebar />
      <main className="app-main">{children}</main>
    </div>
  );
}
