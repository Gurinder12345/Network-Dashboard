"""
Platform adapters for the shared change workflow (changes/workflow.py).

Only what genuinely differs per platform lives here: how the running configuration is
read, how a requested change is validated/normalized, how desired state is verified,
and how commands are written. Approval, cancellation, backup, jobs, audit, state
transitions and error handling are shared.

dell_os6  : unchanged behaviour (same reader, verifier and Ansible runner as before).
dell_os10 : allow-list policy (changes/policy.py), running-configuration verifier
            (changes/os10.py), Ansible runner (ansible/run_os10.py).
"""

from changes import os10 as os10_verify
from changes.policy import check_os10


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

    def read_running_config(self, target_host):
        from ansible.run_os6 import get_show_output

        return get_show_output(target_host, self.running_config_command)

    def verify(self, target_host, config_lines, config_parents, snapshot=None):
        from ansible.run_os6 import run_os6_config_check

        if snapshot is None:
            return run_os6_config_check(target_host, config_lines, config_parents)
        return run_os6_config_check(target_host, config_lines, config_parents, running_config_snapshot=snapshot)

    def apply(self, target_host, config_lines, config_parents):
        from ansible.run_os6 import run_os6_config_apply

        return run_os6_config_apply(target_host, config_lines, config_parents)


class Os10Platform:
    name = "dell_os10"
    label = "Dell OS10"
    running_config_command = "show running-configuration"
    # Idempotency: re-read before writing; if the device already has the desired state,
    # nothing is sent.
    pre_apply_verify = True

    def prepare(self, config_lines, config_parents):
        return check_os10(config_lines, config_parents)

    def read_running_config(self, target_host):
        from tasks.dell_os10 import get_running_config

        result = get_running_config(target_host)[target_host]
        if result["failed"]:
            raise RuntimeError(result["result"])
        return result["result"]

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
