# Interface parser fixtures

**Every file here is SYNTHETIC.** They follow the documented Dell OS6 (N-series 6.x) and
Dell OS10 (10.5) layouts for the interface `show` commands; nothing was captured from this
lab. Real output must be captured (read-only) before scheduled interface polling is enabled:

    python -m interfaces.validate_live capture Kenda-HARO-SW-01 Kenda-Core-2

Copy the saved files here as `os6_real_<command>.txt` / `os10_real_<command>.txt`
(e.g. `os6_real_show_interfaces_status.txt`). `test_interface_parsers.py` parses every
real file it finds and fails if the parser rejects it or returns no interfaces.
