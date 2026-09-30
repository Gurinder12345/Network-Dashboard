"""
Offline LLDP correlation / identity-candidate report. Read-only; contacts no switch and
writes nothing.

    python3 -m topology.identity_report \
        --capture Kenda-Core-1=dell_os10:tests/fixtures/os10_real_show_lldp_neighbors.txt \
        --capture Kenda-HARO-IDF-A=dell_os6:tests/fixtures/os6_real_show_lldp_remote_device_all.txt \
        --inventory-url http://192.168.137.148/api/v1/devices \
        [--identities-json identities.json]

For every parsed neighbor it prints the current correlation result (match type).
Unmatched named neighbors are reported as identity CANDIDATES. A candidate is only
marked CONFIRMED by deterministic evidence: a capture from a managed device B contains
the reciprocal observation -- B's local port == the advertised remote port, and B's
reported remote port == the capturing device's local port. Name similarity is never
used. Confirmed or not, nothing is written: the operator inserts identity rows.
"""

import argparse
import json
import re
import sys
import urllib.request

from topology.correlate import correlate
from topology.identity import normalize_chassis_id
from topology.lldp import parse_lldp_output


def _port(value):
    return re.sub(r"\s+", "", value).lower() if value else None


def load_inventory(args):
    if args.inventory_json:
        with open(args.inventory_json) as handle:
            return json.load(handle)
    with urllib.request.urlopen(args.inventory_url, timeout=10) as response:  # read-only GET
        return [{"id": d["id"], "hostname": d["hostname"], "management_ip": d["management_ip"]} for d in json.load(response)]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--capture", action="append", required=True, help="HOSTNAME=PLATFORM:PATH")
    parser.add_argument("--inventory-url")
    parser.add_argument("--inventory-json")
    parser.add_argument("--identities-json", help="rows: device_id, lldp_system_name, chassis_id")
    args = parser.parse_args()
    if not (args.inventory_url or args.inventory_json):
        parser.error("--inventory-url or --inventory-json is required")

    inventory = load_inventory(args)
    by_host = {d["hostname"].lower(): d for d in inventory}
    identities = json.load(open(args.identities_json)) if args.identities_json else []

    captures = {}
    for spec in args.capture:
        host, rest = spec.split("=", 1)
        platform, path = rest.split(":", 1)
        device = by_host.get(host.lower())
        if device is None:
            sys.exit(f"{host} is not in the inventory")
        neighbors, warnings = parse_lldp_output(platform, open(path).read())
        if warnings:
            sys.exit(f"{host}: parser warnings {warnings} -- fix parsing before correlating")
        captures[device["id"]] = (device, correlate(neighbors, inventory, device["id"], identities))

    print(f"{'SOURCE':18} {'LOCAL IF':17} {'LLDP SYSTEM NAME':22} {'CHASSIS':18} {'REMOTE PORT':18} {'RESULT':30} EVIDENCE")
    candidates = {}

    for device_id, (device, rows) in captures.items():
        for n in rows:
            if n["remote_device_id"]:
                matched = next(d["hostname"] for d in inventory if d["id"] == n["remote_device_id"])
                result, evidence = f"{matched} ({n['match_type']})", "-"
            else:
                result = n["match_type"] + (f": {n['correlation_note']}" if n.get("correlation_note") else "")
                evidence = "no reciprocal capture"
                # Deterministic confirmation: reciprocal port pair in another managed device's capture.
                for other_id, (other, other_rows) in captures.items():
                    if other_id == device_id:
                        continue
                    for r in other_rows:
                        if _port(r["local_interface"]) == _port(n["remote_port_id"]) and _port(r["remote_port_id"]) == _port(n["local_interface"]):
                            evidence = f"CONFIRMED by {other['hostname']} {r['local_interface']} -> {r['remote_port_id']}"
                            result = f"candidate = {other['hostname']}"
                if n["remote_system_name"] or n["remote_chassis_id"]:
                    key = n["remote_system_name"] or normalize_chassis_id(n["remote_chassis_id"])
                    candidates.setdefault(key, []).append((device["hostname"], n, evidence))

            print(f"{device['hostname']:18} {n['local_interface']:17} {str(n['remote_system_name']):22} "
                  f"{str(n['remote_chassis_id']):18} {str(n['remote_port_id']):18} {result:30} {evidence}")

    print("\nUNMATCHED IDENTITIES (operator decision required; nothing is written):")
    for key, seen in candidates.items():
        confirmed = [e for _, _, e in seen if e.startswith("CONFIRMED")]
        chassis = sorted({normalize_chassis_id(n["remote_chassis_id"]) for _, n, _ in seen if n["remote_chassis_id"]})
        ports = [f"{src} {n['local_interface']}->{n['remote_port_id']}" for src, n, _ in seen]
        status = confirmed[0] if confirmed else "needs confirmation"
        print(f"  {key:22} chassis={chassis} seen={ports} status={status}")


if __name__ == "__main__":
    main()
