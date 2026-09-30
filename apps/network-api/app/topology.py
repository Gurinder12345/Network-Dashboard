"""
Topology read APIs and the manual Discover Now trigger.

GET reads: Redis `topology:graph` (graph structure only, TTL 300 s) -> PostgreSQL on a
miss, a Redis error, or when the cached graph predates the worker's last completed
discovery. Current device health is overlaid on every request from the existing health
cache/PostgreSQL, so node colors are never 5 minutes stale. The API never connects to
switches; Discover Now only enqueues the worker task.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app import cache
from app.celery_client import PUBLISH_RETRY_POLICY, celery_app
from app.db.devices import list_devices
from app.db.topology import list_discovery_states, list_links
from app.health import health_by_device
from app.topology_graph import build_topology


logger = logging.getLogger("network_api.topology")

router = APIRouter(prefix="/api/v1/topology", tags=["topology"])

GRAPH_KEY = "topology:graph"
LAST_DISCOVERY_KEY = "topology:last_discovery"
LOCK_KEY = "topology:fleet:lock"
MANUAL_COOLDOWN_KEY = "topology:manual:cooldown"

GRAPH_TTL_SECONDS = 300
MANUAL_COOLDOWN_SECONDS = 45
DISCOVERY_TASK = "network_worker.discover_topology_all_devices"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _build_from_postgres(include_inactive):
    graph = build_topology(list_devices(), list_links(include_inactive), list_discovery_states())
    graph["built_at"] = _now_iso()
    return graph


def _graph_structure(include_inactive):
    """Cached structure for the default (active-only) view; always fresh for inactive."""
    if include_inactive:
        return _build_from_postgres(True)

    cached, last_discovery = cache.mget_json([GRAPH_KEY, LAST_DISCOVERY_KEY])

    if cached is not None:
        finished = (last_discovery or {}).get("finished_at")
        if not finished or finished <= cached.get("built_at", ""):
            return cached

    graph = _build_from_postgres(False)
    cache.set_json(GRAPH_KEY, graph, GRAPH_TTL_SECONDS)
    return graph


def _overlay_health(graph):
    managed_ids = [n["device_id"] for n in graph["nodes"] if n["managed"]]
    health = health_by_device(managed_ids)
    nodes = []

    for node in graph["nodes"]:
        if node["managed"]:
            h = health.get(node["device_id"], {})
            nodes.append({
                **node,
                "health_status": h.get("status", "unknown"),
                "response_time_ms": h.get("response_time_ms"),
                "last_check_at": h.get("last_check_at"),
                "last_error": h.get("last_error"),
            })
        else:
            # Unmanaged neighbors are never health-checked; never imply a state.
            nodes.append({**node, "health_status": "unknown", "response_time_ms": None})

    return {**graph, "nodes": nodes}


@router.get("")
def get_topology(include_inactive: bool = False):
    try:
        graph = _overlay_health(_graph_structure(include_inactive))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        **graph,
        "down_devices": sum(1 for n in graph["nodes"] if n["managed"] and n["health_status"] == "down"),
        "discovery_running": bool(cache.exists(LOCK_KEY)),
        "last_run": cache.get_json(LAST_DISCOVERY_KEY),
    }


@router.get("/devices/{device_id}")
def get_device_topology(device_id: int):
    try:
        graph = _overlay_health(_graph_structure(False))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    node_id = f"device:{device_id}"
    node = next((n for n in graph["nodes"] if n["id"] == node_id), None)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Device {device_id} not in topology")

    nodes = {n["id"]: n for n in graph["nodes"]}
    neighbors = []

    for link in graph["links"]:
        if node_id not in (link["source"], link["target"]):
            continue
        local_is_source = link["source"] == node_id
        other = nodes[link["target"] if local_is_source else link["source"]]
        neighbors.append({
            "link_id": link["id"],
            "local_interface": link["source_interface"] if local_is_source else link["target_interface"],
            "remote_interface": link["target_interface"] if local_is_source else link["source_interface"],
            "remote_port_id": link.get("target_port_id"),
            "remote_node_id": other["id"],
            "remote_hostname": other["hostname"],
            "remote_managed": other["managed"],
            "remote_device_id": other.get("device_id"),
            "remote_health_status": other["health_status"],
            "observed_bidirectionally": link["observed_bidirectionally"],
            "last_seen_at": link["last_seen_at"],
        })

    return {
        "device": node,
        "last_discovery_at": node.get("topology_last_success_at"),
        "last_attempt_at": node.get("topology_last_attempt_at"),
        "last_error": node.get("topology_last_error"),
        "neighbors": sorted(neighbors, key=lambda n: (n["local_interface"] or "")),
    }


@router.post("/discover", status_code=202)
def discover_now():
    if cache.exists(LOCK_KEY):
        return JSONResponse(
            status_code=409,
            content={"status": "running", "detail": "A topology discovery is already running"},
        )

    if cache.claim(MANUAL_COOLDOWN_KEY, MANUAL_COOLDOWN_SECONDS) is False:
        retry_after = cache.ttl(MANUAL_COOLDOWN_KEY) or MANUAL_COOLDOWN_SECONDS
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            content={
                "status": "cooldown",
                "detail": f"Discovery was just requested; try again in {retry_after} s",
                "retry_after": retry_after,
            },
        )

    try:
        task = celery_app.send_task(
            DISCOVERY_TASK,
            kwargs={"trigger": "manual"},
            retry=True,
            retry_policy=PUBLISH_RETRY_POLICY,
            expires=300,
        )
    except Exception as exc:
        logger.exception("Failed to enqueue topology discovery")
        raise HTTPException(status_code=503, detail=f"Task queue unavailable: {exc}")

    logger.info("topology_discovery_manual_requested request_id=%s", task.id)
    return {"status": "queued", "request_id": task.id}
