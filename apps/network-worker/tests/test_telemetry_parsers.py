"""
CPU / memory / uptime parsers against REAL captures (Kenda-HARO-IDF-A OS6 6.8.1,
Kenda-Core-1 OS10 10.5.4). Pure: no devices, DB or Redis.
    python -m unittest tests.test_telemetry_parsers -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telemetry import parsers  # noqa: E402
from telemetry.parsers import parse_metrics  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as handle:
        return handle.read()


OS6 = {
    "show process cpu": fixture("os6_real_show_process_cpu_Kenda-HARO-IDF-A.txt"),
    "show memory cpu": fixture("os6_real_show_memory_cpu_Kenda-HARO-IDF-A.txt"),
    "show system": fixture("os6_real_show_system_Kenda-HARO-IDF-A.txt"),
}
OS10 = {
    "show processes node-id 1": fixture("os10_real_show_processes_node_id_1_Kenda-Core-1.txt"),
    "show uptime": fixture("os10_real_show_uptime_Kenda-Core-1.txt"),
}


def replace(outputs, command, old, new):
    assert old in outputs[command], old
    return {**outputs, command: outputs[command].replace(old, new)}


class CommandSetTests(unittest.TestCase):
    def test_final_command_sets(self):
        self.assertEqual(parsers.TELEMETRY_COMMANDS["dell_os6"], ("show process cpu", "show memory cpu", "show system"))
        self.assertEqual(parsers.TELEMETRY_COMMANDS["dell_os10"], ("show processes node-id 1", "show uptime"))
        for commands in parsers.TELEMETRY_COMMANDS.values():
            self.assertNotIn("show version", commands)
            self.assertTrue(all(c.startswith("show ") for c in commands))


class Os6ParserTests(unittest.TestCase):
    def parse(self, outputs=OS6):
        return parse_metrics("dell_os6", outputs)

    def test_real_fixture_exact_values(self):
        m = self.parse()
        self.assertEqual(m["cpu_percent"], 2.98)  # 60-second Total CPU Utilization
        self.assertAlmostEqual(m["memory_percent"], 2235804 / 3994544 * 100, places=2)
        self.assertAlmostEqual(m["memory_percent"], 55.97, places=2)
        self.assertAlmostEqual(m["memory_total_mb"], 3994544 / 1024, delta=0.05)
        self.assertAlmostEqual(m["memory_used_mb"], 2235804 / 1024, delta=0.05)
        self.assertEqual(m["uptime_seconds"], 201 * 86400 + 4 * 3600 + 13 * 60 + 32)
        self.assertEqual(m["problems"], [])

    def test_cpu_is_total_line_not_sum_of_processes(self):
        rows = [line for line in OS6["show process cpu"].splitlines() if line.strip().endswith("%") and "Total" not in line]
        summed = sum(float(line.split()[3].rstrip("%")) for line in rows if len(line.split()) >= 5)
        self.assertNotAlmostEqual(summed, 2.98, places=2)
        self.assertEqual(self.parse()["cpu_percent"], 2.98)

    def test_missing_total_cpu_line_is_partial(self):
        outputs = replace(OS6, "show process cpu", "Total CPU Utilization", "Overall")
        m = self.parse(outputs)
        self.assertIsNone(m["cpu_percent"])
        self.assertAlmostEqual(m["memory_percent"], 55.97, places=2)
        self.assertIn("CPU unavailable: 'Total CPU Utilization' line not found", m["problems"])

    def test_malformed_cpu(self):
        for bad in ("2.85%    n/a    3.10%", "2.85%    2.98", "2.85%    -2.98%    3.10%"):
            with self.subTest(bad=bad):
                m = self.parse(replace(OS6, "show process cpu", "2.85%    2.98%    3.10%", bad))
                self.assertIsNone(m["cpu_percent"])
                self.assertIsNotNone(m["memory_percent"])

    def test_cpu_over_100_rejected_not_clamped(self):
        m = self.parse(replace(OS6, "show process cpu", "2.85%    2.98%    3.10%", "2.85%  102.98%    3.10%"))
        self.assertIsNone(m["cpu_percent"])
        self.assertIn("out of range", m["problems"][0])

    def test_missing_memory_total_uses_same_report_from_process_cpu(self):
        m = self.parse(replace(OS6, "show memory cpu", "Total Memory", "Memory"))
        self.assertAlmostEqual(m["memory_percent"], 55.97, places=2)  # free/alloc report, same numbers

    def test_missing_memory_everywhere_is_partial(self):
        outputs = replace(OS6, "show memory cpu", "Available Memory Space", "Spare")
        outputs = replace(outputs, "show process cpu", "alloc     2235804", "")
        m = self.parse(outputs)
        self.assertIsNone(m["memory_percent"])
        self.assertIsNone(m["memory_used_mb"])
        self.assertIsNone(m["memory_total_mb"])
        self.assertEqual(m["cpu_percent"], 2.98)
        self.assertIn("Memory unavailable: 'Available Memory Space' line not found", m["problems"])

    def test_malformed_and_inconsistent_memory(self):
        bad_total = replace(OS6, "show memory cpu", "3994544 KBytes", "39945x4 KBytes")
        bad_total = replace(bad_total, "show process cpu", "free      1758740", "")
        self.assertIsNone(self.parse(bad_total)["memory_percent"])
        more_free = replace(OS6, "show memory cpu", "1758740 KBytes", "4994544 KBytes")
        more_free = replace(more_free, "show process cpu", "free      1758740", "")
        m = self.parse(more_free)
        self.assertIsNone(m["memory_percent"])
        self.assertIn("inconsistent", " ".join(m["problems"]))

    def test_malformed_uptime_never_affects_cpu_memory(self):
        for bad in ("System Up Time: 201 days, 04h:13m", "System Up Time: soon", "System Up Time: 201 days, 04h:73m:32s"):
            with self.subTest(bad=bad):
                m = self.parse(replace(OS6, "show system", "System Up Time: 201 days, 04h:13m:32s", bad))
                self.assertIsNone(m["uptime_seconds"])
                self.assertEqual(m["cpu_percent"], 2.98)
                self.assertIsNotNone(m["memory_percent"])
        missing = {**OS6, "show system": OS6["show system"].replace("System Up Time", "Running")}
        self.assertIsNone(self.parse(missing)["uptime_seconds"])

    def test_cli_error_or_empty_output_marks_only_that_source(self):
        m = self.parse({**OS6, "show system": "\n% Invalid input detected at '^' marker.\n"})
        self.assertIsNone(m["uptime_seconds"])
        self.assertEqual(m["cpu_percent"], 2.98)
        m = self.parse({**OS6, "show process cpu": ""})
        self.assertIsNone(m["cpu_percent"])
        self.assertIsNotNone(m["memory_percent"])  # show memory cpu is still fine

    def test_nothing_usable(self):
        m = self.parse({})
        self.assertEqual([m[f] for f in parsers.FIELDS], [None] * 5)
        self.assertEqual(len(m["problems"]), 3)


class Os10ParserTests(unittest.TestCase):
    def parse(self, outputs=OS10):
        return parse_metrics("dell_os10", outputs)

    def test_real_fixture_exact_values(self):
        m = self.parse()
        self.assertEqual(m["cpu_percent"], 17.1)  # 100 - 82.9 idle
        self.assertAlmostEqual(m["memory_percent"], 2473812 / 4012568 * 100, places=2)
        self.assertAlmostEqual(m["memory_percent"], 61.65, places=2)
        self.assertAlmostEqual(m["memory_total_mb"], 4012568 / 1024, delta=0.05)
        self.assertAlmostEqual(m["memory_used_mb"], 2473812 / 1024, delta=0.05)
        self.assertEqual(m["uptime_seconds"], 3 * 86400 + 3 * 3600 + 30 * 60 + 36)
        self.assertEqual(m["problems"], [])

    def test_cpu_is_not_sum_of_process_rows(self):
        lines = OS10["show processes node-id 1"].splitlines()
        start = next(i for i, line in enumerate(lines) if line.strip().startswith("PID"))
        summed = sum(float(line.split()[8]) for line in lines[start + 1:] if len(line.split()) > 8)
        self.assertNotAlmostEqual(summed, 17.1, places=1)

    def test_missing_idle_field(self):
        m = self.parse(replace(OS10, "show processes node-id 1", " 82.9 id,", ""))
        self.assertIsNone(m["cpu_percent"])
        self.assertAlmostEqual(m["memory_percent"], 61.65, places=2)
        self.assertIn("CPU unavailable: idle ('id') not found in %Cpu(s) line", m["problems"])

    def test_malformed_cpu_line(self):
        for old, new in ((" 82.9 id,", " 82,9 id,"), (" 82.9 id,", " 182.9 id,"), ("%Cpu(s):", "Cpu:"),
                         (" 82.9 id,", " 82.9 id, 1.0 id,")):
            with self.subTest(new=new):
                m = self.parse(replace(OS10, "show processes node-id 1", old, new))
                self.assertIsNone(m["cpu_percent"])
                self.assertIsNotNone(m["memory_percent"])

    def test_missing_memory_line(self):
        m = self.parse(replace(OS10, "show processes node-id 1", "KiB Mem :", "Memory:"))
        self.assertIsNone(m["memory_percent"])
        self.assertEqual(m["cpu_percent"], 17.1)
        self.assertIn("Memory unavailable: 'KiB Mem' line not found", m["problems"])

    def test_malformed_or_inconsistent_memory(self):
        for old, new in (("2473812 used", "2473812 utilised"), ("2473812 used", "24738x2 used"),
                         ("2473812 used", "5012568 used"), ("4012568 total,   729204", "0 total,   729204")):
            with self.subTest(new=new):
                m = self.parse(replace(OS10, "show processes node-id 1", old, new))
                self.assertIsNone(m["memory_percent"])
                self.assertEqual(m["cpu_percent"], 17.1)

    def test_swap_line_is_not_memory(self):
        # "KiB Swap ... 0 used" must never be read as memory.
        self.assertAlmostEqual(self.parse()["memory_used_mb"], 2473812 / 1024, delta=0.05)

    def test_malformed_uptime(self):
        for bad in ("3 days 03:30", "three days", "3 days 03:75:36", "% Error: Unrecognized command."):
            with self.subTest(bad=bad):
                m = self.parse({**OS10, "show uptime": bad})
                self.assertIsNone(m["uptime_seconds"])
                self.assertEqual(m["cpu_percent"], 17.1)
        self.assertIsNone(self.parse({"show processes node-id 1": OS10["show processes node-id 1"]})["uptime_seconds"])

    def test_spacing_tolerance(self):
        tight = replace(OS10, "show processes node-id 1", "%Cpu(s):  8.6 us,  8.6 sy,  0.0 ni, 82.9 id,",
                        "%Cpu(s): 8.6 us, 8.6 sy, 0.0 ni,82.9 id,")
        self.assertEqual(self.parse(tight)["cpu_percent"], 17.1)
        self.assertEqual(self.parse({**OS10, "show uptime": "  3 days 03:30:36  \n"})["uptime_seconds"], 271836)

    def test_partial_when_processes_output_missing(self):
        m = self.parse({"show uptime": OS10["show uptime"]})
        self.assertEqual((m["cpu_percent"], m["memory_percent"]), (None, None))
        self.assertEqual(m["uptime_seconds"], 271836)


class CollectorOutcomeTests(unittest.TestCase):
    """Real fixtures through the collector's success/partial/failed classification."""

    def test_outcomes(self):
        from telemetry.collector import status_for

        self.assertEqual(status_for(parse_metrics("dell_os6", OS6)), ("success", None))
        self.assertEqual(status_for(parse_metrics("dell_os10", OS10))[0], "success")
        partial = parse_metrics("dell_os10", replace(OS10, "show processes node-id 1", " 82.9 id,", ""))
        self.assertEqual(status_for(partial)[0], "partial")
        failed = parse_metrics("dell_os6", {})
        self.assertEqual(status_for(failed)[0], "failed")


if __name__ == "__main__":
    unittest.main()
