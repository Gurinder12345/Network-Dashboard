"""
Platform adapters for the shared change workflow (changes/workflow.py).

Only what genuinely differs per platform lives here: how device state is read, how a
requested change is validated/normalized and planned, how desired state is verified, and
how commands are written. Approval, cancellation, backup, jobs, audit, state transitions
and error handling are shared.

dell_os6  : unchanged behaviour (same reader, verifier and Ansible runner as before).
dell_os10 : safe-L2 allow-list (changes/policy.py), planner + safety classification
            (changes/os10_plan.py, changes/os10_safety.py), running-configuration
            verifier (changes/os10.py), Ansible runner (ansible/run_os10.py).
"""

import os
import re

from changes import os10 as os10_verify
from changes import os10_plan, os10_safety
from changes.policy import change_kinds, check_os10, check_os10_commands


class UnsupportedPlatform(ValueError):
    pass


class Os6Platform:
    name = "dell_os6"
    label = "Dell OS6"
    running_config_command = "show running-config"
    # OS6 apply has always relied on precheck + post-check; no extra read before writing.
    pre_apply_verify = False

    def prepare(self, config_lines, config_parents):
        # OS6 accepts any command the API validated (printable, no mode commands).
        return config_lines, config_parents

    def validate_approved(self, config_lines, config_parents):
        return self.prepare(config_lines, config_parents)

    def read_running_config(self, target_host):
        from ansible.run_os6 import get_show_output

        return get_show_output(target_host, self.running_config_command)

    def read_device_state(self, target_host, config_lines):
        return {"running_config": self.read_running_config(target_host)}

    def plan(self, target_host, device, config_lines, config_parents, state):
        return self.verify(target_host, config_lines, config_parents, state["running_config"])

    def verify(self, target_host, config_lines, config_parents, snapshot=None):
        from ansible.run_os6 import run_os6_config_check

        if snapshot is None:
            return run_os6_config_check(target_host, config_lines, config_parents)
        return run_os6_config_check(target_host, config_lines, config_parents, running_config_snapshot=snapshot)

    def apply(self, target_host, config_lines, config_parents):
        from ansible.run_os6 import run_os6_config_apply

        return run_os6_config_apply(target_host, config_lines, config_parents)


# Operator-maintained extra protection, e.g. "Kenda-Core-1:ethernet1/1/25,*:ethernet1/1/26:1".
# Unset = nothing extra; automatic classification (LLDP, port-channel, management, ...) always applies.
PROTECTED_ENV = "OS10_PROTECTED_INTERFACES"
_PROTECTED_ENTRY = re.compile(r"^([A-Za-z0-9._-]+|\*):(ethernet\d+/\d+/\d+(?::\d+)?)$", re.IGNORECASE)


def protected_interfaces(hostname, raw=None):
    """Returns (set of interface names protected on hostname, error or None). Malformed -> error (fail closed)."""
    raw = os.getenv(PROTECTED_ENV, "") if raw is None else raw
    protected = set()
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        match = _PROTECTED_ENTRY.match(entry)
        if not match:
            return set(), f"malformed entry {entry[:60]!r} in {PROTECTED_ENV}"
        if match.group(1) == "*" or match.group(1).lower() == hostname.lower():
            protected.add(match.group(2).lower())
    return protected, None


def _norm_interface(name):
    return "".join((name or "").lower().split())


class Os10Platform:
    name = "dell_os10"
    label = "Dell OS10"
    running_config_command = "show running-configuration"
    # Re-read before writing: re-check safety against the device as it is now, send only
    # what is still needed; if the device already has the desired state, nothing is sent.
    pre_apply_verify = True

    def prepare(self, config_lines, config_parents):
        return check_os10(config_lines, config_parents)

    def validate_approved(self, config_lines, config_parents):
        return check_os10_commands(config_lines, config_parents)

    def read_running_config(self, target_host):
        from tasks.dell_os10 import get_running_config

        result = get_running_config(target_host)[target_host]
        if result["failed"]:
            raise RuntimeError(result["result"])
        return result["result"]

    def read_device_state(self, target_host, config_lines):
        """Running config, plus `show lldp neighbors` in the same SSH session for L2/admin changes."""
        if change_kinds(config_lines) <= {"description"}:
            return {"running_config": self.read_running_config(target_host), "lldp": None,
                    "lldp_error": "not collected (description-only change)"}

        from tasks.dell_os10 import get_running_config_and_lldp

        result = get_running_config_and_lldp(target_host)[target_host]
        if result["failed"]:
            raise RuntimeError(result["result"])
        return {"running_config": result["running_config"], "lldp": result.get("lldp"),
                "lldp_error": result.get("lldp_error")}

    # ---- safety context ---------------------------------------------------------------------
    def _live_neighbors(self, device, interface, state):
        if state.get("lldp") is None:
            return None, state.get("lldp_error") or "not collected"
        from topology.lldp import parse_lldp_output

        try:
            neighbors, warnings = parse_lldp_output(self.name, state["lldp"])
        except Exception as exc:
            return None, f"LLDP output could not be parsed ({type(exc).__name__})"
        if warnings:
            return None, "LLDP output had rows that could not be parsed"
        on_port = [n for n in neighbors if _norm_interface(n.get("local_interface")) == interface]
        if not on_port:
            return [], None
        try:
            from db.topology import list_inventory, list_lldp_identities
            from topology.correlate import correlate

            inventory = list_inventory()
            names = {d["id"]: d["hostname"] for d in inventory}
            resolved = correlate(on_port, inventory, device["id"], list_lldp_identities())
            return [{**n, "remote_hostname": names.get(n.get("remote_device_id"))} for n in resolved], None
        except Exception as exc:
            return None, f"LLDP neighbor could not be resolved against inventory ({type(exc).__name__})"

    def _stored_links(self, device, interface):
        try:
            from db.topology import list_links_for_interface

            return list_links_for_interface(device["id"], interface), None
        except Exception as exc:
            return None, f"stored topology unavailable ({type(exc).__name__})"

    def safety_context(self, device, interface, config_lines, state):
        protected, error = protected_interfaces(device["hostname"])
        ctx = {"hostname": device["hostname"], "management_ip": device.get("management_ip"),
               "protected": protected, "protected_error": error}
        if change_kinds(config_lines) - {"description"}:
            ctx["lldp_neighbors"], ctx["lldp_error"] = self._live_neighbors(device, interface, state)
            ctx["stored_links"], ctx["stored_error"] = self._stored_links(device, interface)
        return ctx

    # ---- workflow hooks -----------------------------------------------------------------------
    def plan(self, target_host, device, config_lines, config_parents, state):
        interface = config_parents[0].split(None, 1)[1]
        ctx = self.safety_context(device, interface, config_lines, state)
        return {"target_host": target_host, "platform": self.name, "dry_run": True,
                **os10_plan.plan(config_lines, config_parents, state["running_config"], ctx)}

    def pre_apply(self, device, config_lines, config_parents):
        """Fresh read; re-run safety and preconditions for the stored commands; then verify."""
        state = self.read_device_state(device["hostname"], config_lines)
        running = state["running_config"]
        current = os10_verify.interface_state(running, config_parents[0])
        ctx = self.safety_context(device, current["interface"], config_lines, state)
        safety = os10_safety.evaluate(change_kinds(config_lines), current, running, ctx)
        reasons = safety["reasons"] + os10_plan.device_preconditions(config_lines, current,
                                                                     os10_verify.existing_vlans(running))
        if reasons:
            raise os10_safety.SafetyRejection(" ".join(reasons))
        return {"target_host": device["hostname"], "platform": self.name, "dry_run": True,
                **os10_verify.verify(config_lines, config_parents, running)}

    def verify(self, target_host, config_lines, config_parents, snapshot=None):
        running = snapshot if snapshot is not None else self.read_running_config(target_host)
        return {"target_host": target_host, "platform": self.name, "dry_run": True,
                **os10_verify.verify(config_lines, config_parents, running)}

    def apply(self, target_host, config_lines, config_parents):
        from ansible.run_os10 import run_os10_config_apply

        return run_os10_config_apply(target_host, config_lines, config_parents)


PLATFORMS = {"dell_os6": Os6Platform(), "dell_os10": Os10Platform()}
SUPPORTED_CHANGE_PLATFORMS = tuple(PLATFORMS)


def platform_for(name):
    adapter = PLATFORMS.get(name)
    if adapter is None:
        raise UnsupportedPlatform(f"Configuration changes are not supported for platform {name}")
    return adapter
