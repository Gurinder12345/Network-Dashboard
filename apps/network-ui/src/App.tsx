import { Route, Routes } from "react-router-dom";
import { AppShell } from "./layout/AppShell";
import { Approvals } from "./pages/Approvals";
import { Backups } from "./pages/Backups";
import { Devices } from "./pages/Devices";
import { Jobs } from "./pages/Jobs";
import { Overview } from "./pages/Overview";
import { PlaceholderPage } from "./pages/PlaceholderPage";

export function App() {
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<Overview />} />
        <Route path="/devices" element={<Devices />} />
        <Route path="/changes" element={<PlaceholderPage title="Changes" />} />
        <Route path="/approvals" element={<Approvals />} />
        <Route path="/jobs" element={<Jobs />} />
        <Route path="/backups" element={<Backups />} />
        <Route path="/audit" element={<PlaceholderPage title="Audit" />} />
        <Route path="/scripts" element={<PlaceholderPage title="Scripts" />} />
      </Routes>
    </AppShell>
  );
}
