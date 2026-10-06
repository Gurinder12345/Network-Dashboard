# Dell OS10 change-workflow fixtures

**All files here are SYNTHETIC** and contain no credentials. They follow the documented
Dell OS10 10.5 running-configuration layout; none of the switchport lines were captured
from the lab.

| File | Used for |
|---|---|
| `running_config_l2.txt` | semantic verification of interface / switchport / global commands (access 1/1/18 VLAN 20, trunk 1/1/19 10,20, shut 1/1/20, ...) |
| `running_config_ambiguous.txt` | ambiguous state is reported as "not available", never guessed |
| `ansible_apply_device_error.txt` | ansible-playbook failure summary parsing |
