import { Route, Routes } from "react-router-dom";
import { AppShell } from "./layout/AppShell";
import { Approvals } from "./pages/Approvals";
import { Audit } from "./pages/Audit";
import { Backups } from "./pages/Backups";
import { Changes } from "./pages/Changes";
import { Devices } from "./pages/Devices";
import { Jobs } from "./pages/Jobs";
import { Overview } from "./pages/Overview";
import { Scripts } from "./pages/Scripts";

export function App() {
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<Overview />} />
        <Route path="/devices" element={<Devices />} />
        <Route path="/changes" element={<Changes />} />
        <Route path="/approvals" element={<Approvals />} />
        <Route path="/jobs" element={<Jobs />} />
        <Route path="/backups" element={<Backups />} />
        <Route path="/audit" element={<Audit />} />
        <Route path="/scripts" element={<Scripts />} />
      </Routes>
    </AppShell>
  );
}
