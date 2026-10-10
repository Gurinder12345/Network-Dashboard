"""
Current-fleet load simulation for Interface Monitoring (NO real switches):
10 simulated switches (8 OS6 + 2 OS10, ~50 interfaces each) answering over real SSH
(tests/fakes, real Netmiko), the real fleet sweep (fleet lock, coordination, bounded
concurrency), real bulk PostgreSQL writes and real Redis snapshot writes. Two sweeps: the
second one 300 s "later" (stored samples shifted back) so utilization is computed.

Opt-in, disposable services only:
    IFACE_LOAD_TEST=1 IFACE_TEST_PG_DSN=postgresql://...:55443/ifmon IFACE_TEST_REDIS_URL=redis://127.0.0.1:56379/4 \
        python -m unittest tests.test_interface_load -v
Prints measured timings. Fake SSH has no switch CPU/CLI latency: the cycle time is a lower
bound for the software path; real-switch timing comes from the first live validation.
"""

import json
import os
import statistics
import sys
import time
import unittest
from unittest import mock
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

DSN = os.getenv("IFACE_TEST_PG_DSN")
REDIS_URL = os.getenv("IFACE_TEST_REDIS_URL")
ENABLED = os.getenv("IFACE_LOAD_TEST") and DSN and REDIS_URL


def _row(widths, values):
    return "".join(str(v).ljust(w) for w, v in zip(widths, values)).rstrip()


def _dashes(widths):
    return "".join(("-" * (w - 1)).ljust(w) for w in widths).rstrip()


def os6_outputs(ports, rx_base):
    names = [f"Gi1/0/{n}" for n in range(1, ports - 3)] + ["Te1/0/1", "Te1/0/2", "Te1/0/3", "Po1"]
    w = [11, 26, 6, 7, 8, 5, 6, 10]
    status = ["", _row(w, ("Port", "Description", "Vlan", "Duplex", "Speed", "Neg", "Link", "Flow Ctrl")),
              _row(w, ("", "", "", "", "", "", "State", "Status")), _dashes(w)]
    for i, name in enumerate(names[:-1]):
        trunk = name.startswith("Te")
        status.append(_row(w, (name, f"PORT-{i}", "Trnk" if trunk else "10", "Full", "10000" if trunk else "1000",
                               "Auto", "Down" if i % 9 == 0 else "Up", "Inactive")))
    cw = [5, 31, 6, 6, 9]
    status += ["", _row(cw, ("Ch", "Description", "Link", "Vlan", "Ch Type")), _row(cw, ("", "", "State", "", "")),
               _dashes(cw), _row(cw, ("Po1", "UPLINK-LAG", "Up", "Trnk", "Dynamic")), ""]
    w2 = [10, 14, 8, 9, 6, 7, 6]
    conf = ["", _row(w2, ("Port", "Description", "Duplex", "Speed", "Neg", "MTU", "Admin")),
            _row(w2, ("", "", "", "", "", "", "State")), _dashes(w2)]
    conf += [_row(w2, (n, "", "Full", "1000", "Auto", "1518", "Down" if i % 17 == 0 else "Up")) for i, n in enumerate(names)]
    w3 = [10, 17, 17, 17, 17]
    counters = ["", _row(w3, ("Port", "InOctets", "InUcastPkts", "InMcastPkts", "InBcastPkts")), _dashes(w3)]
    counters += [_row(w3, (n, rx_base + i * 1000, 10_000 + i, 10, 1)) for i, n in enumerate(names)]
    counters += ["", _row(w3, ("Port", "OutOctets", "OutUcastPkts", "OutMcastPkts", "OutBcastPkts")), _dashes(w3)]
    counters += [_row(w3, (n, rx_base // 2 + i * 1000, 9_000 + i, 10, 1)) for i, n in enumerate(names)]
    w4 = [10, 11, 11, 11, 11, 11, 11]
    errors = ["", _row(w4, ("Port", "Align-Err", "FCS-Err", "Xmit-Err", "Rcv-Err", "UnderSize", "OutDiscard")), _dashes(w4)]
    errors += [_row(w4, (n, 0, 0, 0, 0, 0, 0)) for n in names]
    return {"show interfaces status": "\n".join(status), "show interfaces configuration": "\n".join(conf + [""]),
            "show interfaces counters": "\n".join(counters + [""]), "show interfaces counters errors": "\n".join(errors + [""])}


def os10_outputs(ports, rx_base):
    blocks, rows = [], []
    w = [16, 16, 9, 9, 9, 5, 5, 14]
    for n in range(1, ports + 1):
        name = f"Ethernet 1/1/{n}"
        oper = "down" if n % 11 == 0 else "up"
        blocks += [f"{name} is up, line protocol is {oper}", f"Description: SRV-{n}", "MTU 9216 bytes, IP MTU 9184 bytes",
                   "LineSpeed 25G, Auto-Negotiation on", "Input statistics:",
                   f"     {1000 + n} packets, {rx_base + n * 1000} octets", "     0 CRC, 0 overrun, 0 discarded",
                   "Output statistics:", f"     {2000 + n} packets, {rx_base // 2 + n * 1000} octets",
                   "     0 throttles, 0 discarded, 0 Collisions,  wreds",
                   "Time since last interface status change: 1 weeks 1 days 03:02:50", ""]
        rows.append(_row(w, (f"Eth 1/1/{n}", f"SRV-{n}", oper, "25G", "full", "T" if n % 4 == 0 else "A", "1",
                             "10-20" if n % 4 == 0 else "-")))
    rule = "-" * 98
    status = [rule, _row(w, ("Port", "Description", "Status", "Speed", "Duplex", "Mode", "Vlan", "Tagged-Vlans")), rule]
    return {"show interface": "\n".join(blocks), "show interface status": "\n".join(status + rows + [rule, ""])}


@unittest.skipUnless(ENABLED, "IFACE_LOAD_TEST / IFACE_TEST_PG_DSN / IFACE_TEST_REDIS_URL not set")
class FleetLoadSimulation(unittest.TestCase):
    PORTS = 50

    def test_ten_device_fleet(self):
        import psycopg
        import redis

        from coordination import device_ops as coord
        from db import client
        from fakes.os6_device import FakeOS6
        from fakes.os10_device import FakeOS10
        from health import checks
        from interfaces import cache, collector

        url = urlparse(DSN)
        client.DB_HOST, client.DB_PORT, client.DB_NAME = url.hostname, str(url.port or 5432), url.path.lstrip("/")
        client.DB_USER, client.DB_PASSWORD = url.username, url.password
        pg = psycopg.connect(DSN, autocommit=True)
        self.addCleanup(pg.close)
        with open(os.path.join(HERE, "..", "..", "..", "db", "migrations", "009_interface_monitoring.sql")) as handle:
            pg.execute(handle.read())
        pg.execute("TRUNCATE interface_metrics, interface_inventory RESTART IDENTITY")
        ids = [r[0] for r in pg.execute("SELECT id FROM devices ORDER BY id LIMIT 10")]
        self.assertEqual(len(ids), 10, "disposable DB needs 10 devices")

        r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        r.flushdb()
        self.addCleanup(mock.patch.stopall)
        for target in (coord, cache):
            mock.patch.object(target, "_redis", return_value=r).start()
        mock.patch("health.cache._redis", return_value=r).start()

        class Fake6(FakeOS6):
            def show(self, command):
                return self.outputs.get(command) or super().show(command)

        class Fake10(FakeOS10):
            def show(self, command):
                return self.outputs.get(command) or super().show(command)

        devices, fakes = [], {}
        for index, device_id in enumerate(ids):
            platform = "dell_os10" if index < 2 else "dell_os6"
            fake = (Fake10 if platform == "dell_os10" else Fake6)(hostname=f"sim-{index}")
            fake.outputs = (os10_outputs if platform == "dell_os10" else os6_outputs)(self.PORTS, 10**9)
            self.addCleanup(fake.close)
            fakes[device_id] = fake
            devices.append({"id": device_id, "hostname": f"sim-{index}", "platform": platform,
                            "management_ip": "127.0.0.1", "credential_path": "test/x"})

        from fakes.switch import TEST_PASSWORD, TEST_USERNAME
        from netmiko import ConnectHandler

        def open_session(dev, credentials):
            params = dict(host="127.0.0.1", port=fakes[dev["id"]].port, username=TEST_USERNAME, password=TEST_PASSWORD,
                          secret="", conn_timeout=5, auth_timeout=10, banner_timeout=10, fast_cli=False)
            if dev["platform"] == "dell_os6":
                return checks.HealthDellOS6SSH(**params)
            return ConnectHandler(device_type="dell_os10", **params)

        mock.patch.object(collector, "_open_session", side_effect=open_session).start()
        mock.patch.object(collector, "_tcp_reachable", return_value=(True, None)).start()
        mock.patch.object(collector, "get_device_credentials", return_value={"username": "u", "password": "p"}).start()
        mock.patch.object(collector, "list_enabled_devices", return_value=devices).start()

        timings = {"persist": [], "cache": [], "collect": []}
        real_persist, real_write, real_collect = collector.persist_collection, cache.write_snapshot, collector.collect_device

        def timed(name, fn):
            def wrapper(*a, **k):
                t0 = time.perf_counter()
                try:
                    return fn(*a, **k)
                finally:
                    timings[name].append((time.perf_counter() - t0) * 1000)
            return wrapper

        mock.patch.object(collector, "persist_collection", side_effect=timed("persist", real_persist)).start()
        mock.patch.object(cache, "write_snapshot", side_effect=timed("cache", real_write)).start()
        mock.patch.object(collector, "collect_device", side_effect=timed("collect", real_collect)).start()

        first = collector.run_fleet_interfaces(trigger="load-test")
        self.assertEqual(first["success"], 10, first)
        # 300 s later: shift stored samples back, raise counters, sweep again.
        pg.execute("UPDATE interface_metrics SET collected_at = collected_at - interval '300 seconds'")
        for device_id, fake in fakes.items():
            fake.outputs = (os10_outputs if fake.__class__.__name__ == "Fake10" else os6_outputs)(self.PORTS, 10**9 + 3_750_000_000)
        second = collector.run_fleet_interfaces(trigger="load-test")
        self.assertEqual(second["success"], 10, second)

        connections = {d: f.connections for d, f in fakes.items()}
        self.assertEqual(set(connections.values()), {2})  # one SSH session per device per sweep
        self.assertFalse([l for f in fakes.values() for l in f.config_writes()])  # never configuration mode
        rows = pg.execute("SELECT count(*) FROM interface_metrics").fetchone()[0]
        with_util = pg.execute("SELECT count(*) FROM interface_metrics WHERE rx_utilization_pct IS NOT NULL").fetchone()[0]
        keys = r.keys("interface:latest:device:*")
        sizes = [r.memory_usage(k) for k in keys]
        snapshot = json.loads(r.get(keys[0]))
        print(f"\n    fleet: 10 simulated devices, {second['interfaces_seen']} interfaces per sweep, {rows} metric rows, "
              f"{with_util} with utilization after the second sweep")
        print(f"    sweep duration (concurrency {collector.CONCURRENCY}): first {first['duration_ms']} ms, second {second['duration_ms']} ms")
        print(f"    per device SSH collect+parse: median {statistics.median(timings['collect']):.0f} ms, max {max(timings['collect']):.0f} ms")
        print(f"    per device bulk DB write    : median {statistics.median(timings['persist']):.1f} ms, max {max(timings['persist']):.1f} ms")
        print(f"    per device Redis snapshot   : median {statistics.median(timings['cache']):.2f} ms, max {max(timings['cache']):.2f} ms")
        print(f"    Redis memory per snapshot   : median {statistics.median(sizes) / 1024:.1f} KiB "
              f"({len(snapshot['interfaces'])} interfaces), {len(keys)} snapshot keys")
        self.assertGreater(with_util, 0)


if __name__ == "__main__":
    unittest.main()
