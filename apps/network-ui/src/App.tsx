import { Route, Routes } from "react-router-dom";
import { AppShell } from "./layout/AppShell";
import { Overview } from "./pages/Overview";
import { PlaceholderPage } from "./pages/PlaceholderPage";

export function App() {
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<Overview />} />
        <Route path="/devices" element={<PlaceholderPage title="Devices" />} />
        <Route path="/changes" element={<PlaceholderPage title="Changes" />} />
        <Route path="/approvals" element={<PlaceholderPage title="Approvals" />} />
        <Route path="/jobs" element={<PlaceholderPage title="Jobs" />} />
        <Route path="/backups" element={<PlaceholderPage title="Backups" />} />
        <Route path="/audit" element={<PlaceholderPage title="Audit" />} />
        <Route path="/scripts" element={<PlaceholderPage title="Scripts" />} />
      </Routes>
    </AppShell>
  );
}
