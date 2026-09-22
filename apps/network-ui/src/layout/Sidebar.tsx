import { NavLink } from "react-router-dom";

const NAV_ITEMS = [
  { to: "/", label: "Overview" },
  { to: "/devices", label: "Devices" },
  { to: "/changes", label: "Changes" },
  { to: "/approvals", label: "Approvals" },
  { to: "/jobs", label: "Jobs" },
  { to: "/backups", label: "Backups" },
  { to: "/audit", label: "Audit" },
  { to: "/scripts", label: "Scripts" },
];

export function Sidebar() {
  return (
    <nav className="app-sidebar">
      <div className="nav-section-label">Platform</div>
      {NAV_ITEMS.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.to === "/"}
          className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}
        >
          {item.label}
        </NavLink>
      ))}
    </nav>
  );
}
