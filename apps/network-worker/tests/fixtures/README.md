# LLDP parser fixtures

**Every fixture here is SYNTHETIC.** They follow the documented Dell OS6 / OS10 table
layouts but were not captured from this lab. Replace or supplement them with real output:

- `os10_real_show_lldp_neighbors.txt` — from Kenda-Core-1: `show lldp neighbors`
- `os6_real_show_lldp_remote_device_all.txt` — from Kenda-HARO-IDF-A: `show lldp remote-device all`

When a real file exists, `test_lldp_parsers.py` parses it and fails if the parser
raises or skips rows, so a format mismatch is caught before fleet discovery.
