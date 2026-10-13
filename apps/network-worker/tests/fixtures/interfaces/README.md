# Interface parser fixtures

**REAL (Dell OS6):** `os6_real_show_interfaces_*.txt` -- captured read-only from
Kenda-HARO-SW-01 (N-series 6.x) with `interfaces.validate_live capture`, copied unchanged
(no credentials appear in these commands; descriptions are the switch's own port labels).
Key facts they prove: physical-port counters are packets only (octets exist only in the
port-channel tables), mode is the status table's `M` column, trunk VLANs are
`(native),tagged`, and status truncates descriptions to 15 characters.

**REAL (Dell OS6 byte counters):** `os6_real_show_statistics_gigabitethernet_1_0_2.txt`
-- `show statistics gigabitethernet 1/0/2` from Kenda-HARO-SW-01: "Total Packets Received
(Octets)" / "Total Packets Transmitted (Octets)" are the octet totals used for OS6
utilization. (`show rmon statistics` gives received octets only; not used.)

**REAL (Dell OS10):** `os10_real_show_interface*.txt` -- Kenda-Core-2 (61 interfaces,
60 with byte counters, 26 up with a known speed).

**SYNTHETIC:** every other file. They follow the documented OS6 / OS10 (10.5) layouts and
keep edge cases (port-channel members, octet columns, CLI errors) that the real capture
does not contain. A real `show statistics tengigabitethernet` capture is still to be added
(the Te test uses the validated Gi layout). Capture more with:

    python -m interfaces.validate_live capture Kenda-HARO-SW-01 Kenda-Core-2

Copy the saved files here as `os6_real_<command>.txt` / `os10_real_<command>.txt`
(e.g. `os6_real_show_interfaces_status.txt`). `test_interface_parsers.py` parses every
real file it finds and fails if the parser rejects it or returns no interfaces.
