import { NavLink } from "react-router-dom";
import { useFleetHealth } from "../hooks/FleetHealthContext";

type NavItem = { to: string; label: string; badge?: "attention" };

const NAV_GROUPS: { label: string; items: NavItem[] }[] = [
  {
    label: "Monitor",
    items: [
      { to: "/", label: "Overview" },
      { to: "/devices", label: "Devices", badge: "attention" },
      { to: "/topology", label: "Topology" },
    ],
  },
  {
    label: "Change control",
    items: [
      { to: "/changes", label: "Changes" },
      { to: "/approvals", label: "Approvals" },
      { to: "/jobs", label: "Jobs" },
    ],
  },
  {
    label: "Records",
    items: [
      { to: "/backups", label: "Backups" },
      { to: "/audit", label: "Audit" },
    ],
  },
  {
    label: "Automation",
    items: [{ to: "/scripts", label: "Scripts" }],
  },
];

export function Sidebar() {
  const { fleet } = useFleetHealth();
  const attention = fleet ? fleet.down + fleet.degraded : 0;

  return (
    <nav className="app-sidebar" aria-label="Main">
      {NAV_GROUPS.map((group) => (
        <div className="nav-group" key={group.label}>
          <div className="nav-section-label">{group.label}</div>
          {group.items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}
            >
              {item.label}
              {item.badge === "attention" && attention > 0 && (
                <span className="nav-count" title={`${attention} device(s) degraded or down`}>
                  {attention}
                </span>
              )}
            </NavLink>
          ))}
        </div>
      ))}
    </nav>
  );
}
