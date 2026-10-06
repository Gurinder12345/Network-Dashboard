"""
Platform adapters for the shared change workflow (changes/workflow.py).

Both platforms use the same ordered configuration-block model, the same precheck,
backup, approval, apply and post-check workflow. Only execution details differ:

    dell_os6  : `configure`, enable (become) on login, `show running-config`, `show vlan`
    dell_os10 : `configure terminal`, no enable step, `show running-configuration`

There is no configuration command policy in either adapter: the device is the syntax
authority. Reads use Netmiko/Nornir; writes use Ansible (ansible/playbooks/config_blocks_apply.yml).
"""

from changes.blocks import apply_steps
from changes.verify import needs_vlan_table, parse_os6_vlan_ids


class UnsupportedPlatform(ValueError):
    pass


class Os6Platform:
    name = "dell_os6"
    label = "Dell OS6"
    configure_command = "configure"
    running_config_command = "show running-config"

    def read_running_config(self, target_host):
        from ansible.run_os6 import get_show_output

        return get_show_output(target_host, self.running_config_command)

    def read_device_state(self, target_host, blocks):
        from ansible.run_os6 import get_show_output

        state = {"running_config": self.read_running_config(target_host), "vlan_ids": None}
        if needs_vlan_table(self.name, blocks):
            state["vlan_ids"] = parse_os6_vlan_ids(get_show_output(target_host, "show vlan"))
        return state

    def apply_blocks(self, target_host, blocks):
        from ansible.run_os6 import run_os6_blocks_apply

        steps = apply_steps(blocks, self.configure_command)
        return steps, run_os6_blocks_apply(target_host, steps)

    def run_show_commands(self, target_host, commands):
        from tasks.dell_os6 import run_show_commands

        return run_show_commands(target_host, commands)


class Os10Platform:
    name = "dell_os10"
    label = "Dell OS10"
    configure_command = "configure terminal"
    running_config_command = "show running-configuration"

    def read_running_config(self, target_host):
        from tasks.dell_os10 import get_running_config

        result = get_running_config(target_host)[target_host]
        if result["failed"]:
            raise RuntimeError(result["result"])
        return result["result"]

    def read_device_state(self, target_host, blocks):
        return {"running_config": self.read_running_config(target_host), "vlan_ids": None}

    def apply_blocks(self, target_host, blocks):
        from ansible.run_os10 import run_os10_blocks_apply

        steps = apply_steps(blocks, self.configure_command)
        return steps, run_os10_blocks_apply(target_host, steps)

    def run_show_commands(self, target_host, commands):
        from tasks.dell_os10 import run_show_commands

        return run_show_commands(target_host, commands)


PLATFORMS = {"dell_os6": Os6Platform(), "dell_os10": Os10Platform()}
SUPPORTED_CHANGE_PLATFORMS = tuple(PLATFORMS)


def platform_for(name):
    adapter = PLATFORMS.get(name)
    if adapter is None:
        raise UnsupportedPlatform(f"Configuration changes are not supported for platform {name}")
    return adapter
