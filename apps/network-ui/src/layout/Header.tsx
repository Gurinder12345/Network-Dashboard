import kendaLogo from "../assets/kenda-logo.png";
import { isHealthStale, useFleetHealth } from "../hooks/FleetHealthContext";
import { formatRelative } from "../utils/format";

/**
 * Compact system status built only from data the API already returns:
 * API reachability (last poll result) and the stored fleet health summary.
 */
export function Header() {
  const { fleet, error, lastSuccessAt } = useFleetHealth();

  const apiOk = error === null && lastSuccessAt !== null;
  const stale = fleet ? isHealthStale(fleet.last_updated) : false;
  const attention = fleet ? fleet.down + fleet.degraded : 0;

  return (
    <header className="app-header">
      <div className="header-left">
        <div className="brand">
          {/* Light plate: the logo's black lettering would disappear on the dark header. */}
          <span className="brand-logo">
            <img src={kendaLogo} alt="Kenda" width={92} height={18} />
          </span>
          <span className="brand-divider" aria-hidden="true" />
          <span className="brand-name">
            Network Management <span className="brand-sub">Platform</span>
          </span>
        </div>
        <span className="env-tag" title="Lab environment">
          Lab
        </span>
      </div>

      <div className="header-status" aria-label="System status">
        <span className="status-pill" title={error ?? "Last health poll succeeded"}>
          <span className={`tone-dot tone-${apiOk ? "success" : lastSuccessAt ? "danger" : "neutral"}`} />
          API <strong>{apiOk ? "online" : lastSuccessAt ? "unreachable" : "checking"}</strong>
        </span>

        {fleet && (
          <span className="status-pill" title="Enabled devices, from the latest health checks">
            <span
              className={`tone-dot tone-${fleet.down > 0 ? "danger" : attention > 0 ? "warning" : fleet.last_updated ? "success" : "neutral"}`}
            />
            Fleet{" "}
            <strong>
              {fleet.healthy}/{fleet.total}
            </strong>{" "}
            healthy
            {fleet.down > 0 && <> · {fleet.down} down</>}
            {fleet.degraded > 0 && <> · {fleet.degraded} degraded</>}
          </span>
        )}

        {fleet && (
          <span className="status-pill optional" title={fleet.last_updated ?? "No health check has completed"}>
            <span className={`tone-dot tone-${stale ? "warning" : fleet.last_updated ? "info" : "neutral"}`} />
            Health {fleet.last_updated ? formatRelative(fleet.last_updated) : "not checked yet"}
          </span>
        )}
      </div>
    </header>
  );
}
