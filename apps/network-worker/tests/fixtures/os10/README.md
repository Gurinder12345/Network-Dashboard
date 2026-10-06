# Dell OS10 change-workflow fixtures

**All files here are SYNTHETIC** and contain no credentials. They follow the documented
Dell OS10 10.5 running-configuration layout. The LLDP table copies the real column layout
from `../os10_real_show_lldp_neighbors.txt`. None of the switchport lines were captured from
the lab: confirm them with the read-only Stage 0 of the live validation plan
(`python -m changes.validate_os10_live`) before relying on them.

| File | Covers |
|---|---|
| `running_config_description_absent.txt` / `_present.txt` | V1 description changes |
| `running_config_l2.txt` | access port (1/1/18, VLAN 20), trunk (1/1/19, 10,20), shut port with description (1/1/20), LLDP uplink trunk (1/1/25), port-channel member (1/1/30:2), endpoint LLDP neighbor (1/1/33), port used for the protected-list tests (1/1/40), VLTi ports (1/1/49-50), routed port (1/1/54); VLANs 1,10,20,30,40 exist (200 does not); out-of-band management on mgmt1/1/1 |
| `running_config_inband_mgmt.txt` | same, but the management IP is on in-band VLAN 10 |
| `running_config_ambiguous.txt` | conflicting admin lines, two access VLANs, unparseable allowed list, no admin/switchport lines, unrecognised switchport line |
| `show_lldp_neighbors_l2.txt` | LLDP neighbors on 1/1/25 (core peer), 1/1/30:2 (array), 1/1/33 (endpoint) |
| `ansible_apply_*.txt` | ansible-playbook output (success / device error) |
