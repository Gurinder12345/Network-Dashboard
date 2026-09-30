import type { ReactNode } from "react";

interface PageHeaderProps {
  title: string;
  subtitle?: string;
  lastUpdated?: Date | null;
  actions?: ReactNode;
}

export function PageHeader({ title, subtitle, lastUpdated, actions }: PageHeaderProps) {
  return (
    <div className="page-toolbar">
      <div>
        <h1 className="page-title">{title}</h1>
        {subtitle && <p className="page-subtitle">{subtitle}</p>}
      </div>
      {(actions || lastUpdated !== undefined) && (
        <div className="refresh-control">
          {lastUpdated !== undefined && (
            <span className="last-refreshed">
              Updated {lastUpdated ? lastUpdated.toLocaleTimeString() : "—"}
            </span>
          )}
          {actions}
        </div>
      )}
    </div>
  );
}
