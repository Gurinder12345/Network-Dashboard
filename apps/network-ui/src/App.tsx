import { lazy, Suspense } from "react";
import { Route, Routes } from "react-router-dom";
import { TableSkeleton } from "./components/Feedback";
import { AppShell } from "./layout/AppShell";
import { Approvals } from "./pages/Approvals";
import { Audit } from "./pages/Audit";
import { Backups } from "./pages/Backups";
import { Changes } from "./pages/Changes";
import { Devices } from "./pages/Devices";
import { Jobs } from "./pages/Jobs";
import { Overview } from "./pages/Overview";
import { Scripts } from "./pages/Scripts";

// Cytoscape is only downloaded when the Topology page is opened.
const Topology = lazy(() => import("./pages/Topology"));
// Device detail (telemetry charts) is only downloaded when a device is opened.
const DeviceDetail = lazy(() => import("./pages/DeviceDetail"));
// PCAP analyzer page (upload + report) is only downloaded when opened.
const PacketAnalysis = lazy(() => import("./pages/PacketAnalysis"));

export function App() {
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<Overview />} />
        <Route path="/devices" element={<Devices />} />
        <Route
          path="/devices/:deviceId"
          element={
            <Suspense fallback={<TableSkeleton rows={6} columns={4} />}>
              <DeviceDetail />
            </Suspense>
          }
        />
        <Route
          path="/topology"
          element={
            <Suspense fallback={<TableSkeleton rows={6} columns={4} />}>
              <Topology />
            </Suspense>
          }
        />
        <Route
          path="/packet-analysis"
          element={
            <Suspense fallback={<TableSkeleton rows={6} columns={4} />}>
              <PacketAnalysis />
            </Suspense>
          }
        />
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
