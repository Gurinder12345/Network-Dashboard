import { NavLink } from "react-router-dom";
import { Icon, type IconName } from "../components/Icon";
import { useFleetHealth } from "../hooks/FleetHealthContext";

type NavItem = { to: string; label: string; icon: IconName; badge?: "attention" };

const NAV_GROUPS: { label: string; items: NavItem[] }[] = [
  {
    label: "Monitor",
    items: [
      { to: "/", label: "Overview", icon: "home" },
      { to: "/devices", label: "Devices", icon: "server", badge: "attention" },
      { to: "/topology", label: "Topology", icon: "topology" },
    ],
  },
  {
    label: "Change control",
    items: [
      { to: "/changes", label: "Changes", icon: "wrench" },
      { to: "/approvals", label: "Approvals", icon: "checkCircle" },
      { to: "/jobs", label: "Jobs", icon: "jobs" },
    ],
  },
  {
    label: "Records",
    items: [
      { to: "/backups", label: "Backups", icon: "archive" },
      { to: "/audit", label: "Audit", icon: "audit" },
    ],
  },
  {
    label: "Automation",
    items: [
      { to: "/scripts", label: "Scripts", icon: "terminal" },
      { to: "/packet-analysis", label: "Packet Analysis", icon: "pulse" },
    ],
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
              <Icon name={item.icon} size={17} className="nav-icon" />
              <span className="nav-label">{item.label}</span>
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
