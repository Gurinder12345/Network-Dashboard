"""
PCAP analyzer: TShark execution safety, extraction, rules, dual-capture correlation, task
lifecycle and cleanup. Uses the small SYNTHETIC fixtures in tests/fixtures/pcap
(regenerate with tests/fixtures/pcap/make_fixtures.py). Needs tshark, so run inside the
worker image:
    python -m unittest tests.test_pcap_analyzer -v
"""

import ast
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from pcap import analyze, correlate, extract, report, task, tshark  # noqa: E402
from pcap.tshark import TsharkError  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "pcap")
HAVE_TSHARK = os.path.exists(tshark.TSHARK)


def fixture(name):
    return os.path.join(FIX, name)


def single(name):
    return analyze.run({"client": fixture(name)}, "single")


def dual(prefix):
    return analyze.run({"client": fixture(f"{prefix}_client.pcap"), "server": fixture(f"{prefix}_server.pcap")}, "dual")


def titles(result):
    return [f["title"] for f in result["findings"]]


class SubprocessSafetyTests(unittest.TestCase):
    """Static + runtime checks that no shell is ever involved."""

    def test_no_shell_true_anywhere_in_pcap_code(self):
        for name in os.listdir(os.path.join(os.path.dirname(HERE), "pcap")):
            if not name.endswith(".py"):
                continue
            source = open(os.path.join(os.path.dirname(HERE), "pcap", name)).read()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        if kw.arg == "shell":
                            self.fail(f"{name}: shell= passed to a call")
                if isinstance(node, ast.Attribute) and node.attr in ("system", "popen") and getattr(node.value, "id", "") == "os":
                    self.fail(f"{name}: os.{node.attr} used")

    def test_commands_are_argument_arrays_with_the_path_as_one_element(self):
        hostile = "/tmp/x; rm -rf ~ $(id) `id`.pcap"
        with mock.patch.object(tshark.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="File type\tNumber of packets\npcap\t1\n", stderr="")
            tshark.capinfos(hostile)
        args, kwargs = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertEqual(args[0][-2:], ["--", hostile])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(set(kwargs["env"]), {"PATH", "HOME", "LANG", "LC_ALL"})

        with mock.patch.object(tshark.subprocess, "Popen") as popen:
            proc = popen.return_value
            proc.stdout = iter([])
            proc.stderr.read.return_value = ""
            proc.poll.return_value = 0
            proc.returncode = 0
            list(tshark.iter_fields(hostile, ["frame.number"]))
        cmd = popen.call_args.args[0]
        self.assertIsInstance(cmd, list)
        self.assertEqual(cmd[:4], [tshark.TSHARK, "-n", "-r", hostile])
        self.assertNotIn("shell", popen.call_args.kwargs)

    @unittest.skipUnless(HAVE_TSHARK, "tshark not installed")
    def test_hostile_filename_is_analysed_as_a_plain_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a; touch pwned $(id).pcap")
            shutil.copy(fixture("tcp_normal.pcap"), path)
            result = analyze.run({"client": path}, "single")
            self.assertEqual(result["observations"]["captures"]["client"]["summary"]["packets"], 10)
            self.assertEqual(sorted(os.listdir(tmp)), ["a; touch pwned $(id).pcap"])

    def test_dumpcap_never_used(self):
        for name in os.listdir(os.path.join(os.path.dirname(HERE), "pcap")):
            if name.endswith(".py"):
                with open(os.path.join(os.path.dirname(HERE), "pcap", name)) as handle:
                    self.assertNotIn("dumpcap", handle.read().replace("dumpcap is not used", ""))


@unittest.skipUnless(HAVE_TSHARK, "tshark not installed")
class CaptureValidationTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(analyze.detect_format(fixture("tcp_normal.pcap")), "pcap")
        self.assertEqual(analyze.detect_format(fixture("tcp_normal.pcapng")), "pcapng")
        self.assertIsNone(analyze.detect_format(fixture("not_a_capture.pcap")))

    def test_metadata(self):
        meta = single("tcp_normal.pcapng")["observations"]["captures"]["client"]["metadata"]
        self.assertEqual((meta["file_type"], meta["encapsulation"], meta["packets"], meta["detected_format"]),
                         ("pcapng", "ether", 10, "pcapng"))
        self.assertAlmostEqual(meta["duration_s"], 0.1412, places=4)
        self.assertGreater(meta["avg_packet_rate"], 0)
        self.assertTrue(meta["first_packet"] and meta["last_packet"])

    def test_pcap_and_pcapng_give_the_same_analysis(self):
        a, b = single("tcp_normal.pcap"), single("tcp_normal.pcapng")
        self.assertEqual(a["flows"], b["flows"])
        self.assertEqual(a["findings"], b["findings"])

    def test_malformed_captures_fail_cleanly(self):
        with self.assertRaisesRegex(TsharkError, "not a pcap or pcapng"):
            single("not_a_capture.pcap")
        with self.assertRaisesRegex(TsharkError, "cut short|could not be read|no packets"):
            single("truncated.pcap")

    def test_errors_never_reveal_the_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "secret-location.pcap")
            shutil.copy(fixture("truncated.pcap"), path)
            with self.assertRaises(TsharkError) as ctx:
                analyze.run({"client": path}, "single")
            self.assertNotIn(tmp, str(ctx.exception))


class TsharkFailureTests(unittest.TestCase):
    def fake(self, body):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "fake-tshark")
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)
        return path

    def test_timeout_kills_and_fails(self):
        with mock.patch.object(tshark, "TSHARK", self.fake("sleep 30")), mock.patch.object(tshark, "TIMEOUT_SECONDS", 1):
            start = time.monotonic()
            with self.assertRaisesRegex(TsharkError, "timed out"):
                list(tshark.iter_fields("/data/cap.pcap", ["frame.number"]))
            self.assertLess(time.monotonic() - start, 10)

    def test_non_zero_exit_is_a_clean_error_without_path(self):
        # argv: -n -r <path> ...  -> the path is $3
        script = self.fake('echo "tshark: The file \\"$3\\" appears damaged" >&2; exit 2')
        with mock.patch.object(tshark, "TSHARK", script):
            with self.assertRaises(TsharkError) as ctx:
                list(tshark.iter_fields("/very/private/path.pcap", ["frame.number"]))
        self.assertIn("<capture>", str(ctx.exception))
        self.assertNotIn("/very/private", str(ctx.exception))


@unittest.skipUnless(HAVE_TSHARK, "tshark not installed")
class SingleCaptureRuleTests(unittest.TestCase):
    def test_normal_tcp_has_no_findings(self):
        r = single("tcp_normal.pcap")
        self.assertEqual(r["findings"], [])
        self.assertEqual(r["assessment"]["label"], "none")
        self.assertIn("No transport, DNS, TLS or ICMP problems", r["assessment"]["summary"])
        flow = r["flows"][0]
        self.assertEqual((flow["client"], flow["server"], flow["client_inferred_by"]),
                         ({"ip": "10.1.1.10", "port": 50000}, {"ip": "10.2.2.20", "port": 80}, "syn"))
        self.assertEqual(flow["handshake"]["state"], "complete")
        self.assertAlmostEqual(flow["handshake"]["syn_to_synack_ms"], 20.0, places=3)
        self.assertAlmostEqual(flow["timing"]["request_to_first_response_ms"], 35.0, places=3)
        self.assertEqual(flow["server_to_client"]["payload_bytes"], 1200)

    def test_retransmissions_and_dup_acks(self):
        r = single("tcp_retransmission.pcap")
        f = next(f for f in r["findings"] if f["cause"] == "packet_loss")
        self.assertEqual((f["evidence"]["retransmissions"], f["evidence"]["fast_retransmissions"], f["evidence"]["duplicate_acks"]), (2, 1, 3))
        self.assertEqual(f["strength"], "strong")
        self.assertIn("consistent with packet loss or reordering", f["interpretation"])
        self.assertNotRegex(json.dumps(r["findings"]).lower(), r"definitely|guaranteed")
        self.assertEqual(r["assessment"]["cause"], "packet_loss")

    def test_reset_from_server(self):
        r = single("tcp_reset.pcap")
        f = r["findings"][0]
        self.assertEqual((f["title"], f["evidence"]["from"], f["cause"]), ("TCP reset from server", "server", "tcp_reset"))
        self.assertIn("before any response", f["observed"])

    def test_unanswered_syns(self):
        r = single("tcp_syn_no_answer.pcap")
        f = r["findings"][0]
        self.assertEqual((f["severity"], f["cause"], f["evidence"]["syn_packets"]), ("critical", "connection_failure", 3))
        self.assertNotIn("packet_loss", [x["cause"] for x in r["findings"]])  # SYN retries are not "data loss"

    def test_zero_window(self):
        r = single("tcp_zero_window.pcap")
        f = r["findings"][0]
        self.assertEqual((f["cause"], f["evidence"]["receiver"], f["evidence"]["events"]), ("receiver_window", "client", 2))

    def test_possible_application_delay_is_hedged(self):
        r = single("app_delay.pcap")
        f = r["findings"][0]
        self.assertEqual((f["title"], f["strength"], f["confidence"]), ("Possible server/application response delay", "supporting", "medium"))
        self.assertAlmostEqual(f["evidence"]["streams"][0]["request_to_first_response_ms"], 1818.0, places=3)
        self.assertEqual(r["assessment"]["label"], "possible")
        self.assertIn("server-side capture would confirm", r["assessment"]["summary"])

    def test_dns_success(self):
        r = single("dns_ok.pcap")
        self.assertEqual(r["findings"], [])
        t = r["dns"]["transactions"][0]
        self.assertEqual((t["name"], t["rcode"], t["latency_ms"], t["answered"]), ("app.example.lab", "NOERROR", 4.0, True))

    def test_dns_problems(self):
        r = single("dns_problems.pcap")
        by_title = {f["title"]: f for f in r["findings"]}
        self.assertEqual(by_title["NXDOMAIN returned"]["evidence"]["names"], ["nosuchhost.example.lab"])
        self.assertEqual(by_title["NXDOMAIN returned"]["severity"], "info")
        self.assertEqual(by_title["DNS server failure responses"]["evidence"]["names"], ["broken.example.lab"])
        self.assertIn("DNS response took 1.25 s", by_title)
        self.assertEqual(by_title["DNS query repeated before response"]["evidence"]["queries"][0]["repeats"], 1)
        self.assertNotIn("DNS query without response", by_title)  # capture ended right after that query
        self.assertEqual(r["assessment"]["label"], "multiple")

    def test_nxdomain_alone_is_never_a_headline(self):
        rep = {"tcp_flows": [], "dns_all": [{"answered": True, "rcode_name": "NXDOMAIN", "name": "x.lab", "latency": 0.01,
                                             "repeats": 0, "capture_after_query_s": 9, "server": ("10.3.3.53", 53)}],
               "icmp": [], "summary": {"duration_s": 10}}
        from pcap import rules
        findings = rules.number(rules.run_single(rep))
        self.assertEqual(rules.assess(findings)["label"], "none")

    def test_tls(self):
        r = single("tls_sessions.pcap")
        sessions = {s["sni"]: s for s in r["tls"]["sessions"]}
        self.assertEqual(sessions["app.example.lab"]["negotiated_version"], "TLS 1.3")
        self.assertIsNotNone(sessions["app.example.lab"]["client_hello_to_server_hello_ms"])
        self.assertEqual(sessions["legacy.example.lab"]["alerts"][0]["description"], "handshake_failure")
        self.assertEqual(titles(r), ["TLS alert: handshake_failure (from server)"])  # the RST after it is not a 2nd issue
        self.assertEqual(r["assessment"]["cause"], "tls_failure")

    def test_icmp_fragmentation_needed(self):
        r = single("icmp_frag_needed.pcap")
        f = r["findings"][0]
        self.assertEqual((f["cause"], f["evidence"]["mtu"], f["evidence"]["events"][0]["original_dst"]), ("mtu", [1400], "10.2.2.20"))
        self.assertIn("ICMP time exceeded", titles(r))
        self.assertEqual(r["summary"] if "summary" in r else r["observations"]["captures"]["client"]["summary"]["icmp_events"], 2)
        # Embedded headers inside ICMP are not counted as extra TCP traffic.
        self.assertEqual(r["observations"]["captures"]["client"]["summary"]["tcp_streams"], 1)

    def test_recommendations_are_linked_to_findings(self):
        r = single("tcp_zero_window.pcap")
        self.assertTrue(r["recommendations"])
        ids = {f["id"] for f in r["findings"]}
        for rec in r["recommendations"]:
            self.assertTrue(set(rec["findings"]) <= ids)
            self.assertEqual(rec["cause"], "receiver_window")
        self.assertEqual(single("tcp_normal.pcap")["recommendations"], [])

    def test_result_never_contains_payload(self):
        for name in ("tcp_normal.pcap", "tls_sessions.pcap", "dns_problems.pcap"):
            blob = json.dumps(single(name))
            self.assertNotIn("GET /status", blob)
            self.assertNotIn("Host:", blob)
            self.assertNotIn("http.", blob)

    def test_port_heuristic_is_labelled(self):
        ext = extract.extract(fixture("tcp_normal.pcap"))
        s = next(iter(ext["tcp"].values()))
        s.syn_src = s.synack_src = None
        client, how = report.infer_client(s)
        self.assertEqual((client, how), (("10.1.1.10", 50000), "port_heuristic"))


@unittest.skipUnless(HAVE_TSHARK, "tshark not installed")
class DualCaptureTests(unittest.TestCase):
    def test_server_delay_with_clock_offset(self):
        r = dual("dual_delay")
        clock = r["correlation"]["clock"]
        self.assertAlmostEqual(clock["estimated_clock_offset_ms"], 5000.0, delta=1.0)
        self.assertEqual(clock["confidence"], "high")
        self.assertAlmostEqual(clock["path_rtt_between_capture_points_ms"], 20.0, delta=1.0)
        flow = r["correlation"]["flows"][0]
        self.assertEqual((flow["matched_by"], flow["missing_at_server"], flow["missing_at_client"]), ("5-tuple", 0, 0))
        self.assertAlmostEqual(flow["server_side_response_ms"], 1800.0, delta=1.0)
        self.assertEqual(r["assessment"]["cause"], "app_delay")
        self.assertIn("Delay at the server/application side", titles(r))
        self.assertNotIn("Possible server/application response delay", titles(r))  # superseded by the dual evidence
        self.assertIn("server/application processing delay rather than transport loss", r["assessment"]["summary"])
        self.assertIn("between the capture points".split()[0], r["assessment"]["summary"].lower() + " between")

    def test_packets_missing_on_each_side(self):
        r = dual("dual_loss")
        flow = r["correlation"]["flows"][0]
        self.assertEqual((flow["missing_at_server"], flow["missing_at_client"]), (1, 1))
        self.assertEqual(flow["missing_at_server_examples"][0]["payload_bytes"], len(b"GET /status HTTP/1.1\r\nHost: app.example.lab\r\n\r\n"))
        self.assertEqual(flow["missing_at_client_examples"][0]["payload_bytes"], 600)
        by_title = {f["title"]: f for f in r["findings"]}
        self.assertIn("between the capture points", by_title["Client packets did not reach the server capture point"]["interpretation"])
        self.assertIn("reverse", by_title["Server packets did not reach the client capture point"]["interpretation"])
        self.assertEqual(r["assessment"]["cause"], "packet_loss")

    def test_nat_fallback_matches_by_initial_sequence(self):
        c_ext = extract.extract(fixture("dual_delay_client.pcap"), collect_segments=True)
        s_ext = extract.extract(fixture("dual_delay_server.pcap"), collect_segments=True)
        c_rep, s_rep = report.build(c_ext, {}), report.build(s_ext, {})
        # Pretend a NAT rewrote the client address/port before the server capture point.
        flow = s_rep["tcp_flows"][0]
        old = (flow["client"]["ip"], flow["client"]["port"])
        flow["client"] = {"ip": "192.0.2.1", "port": 61000}
        s_ext["segments"] = [(seg[0], ("192.0.2.1", 61000) if seg[1] == old else seg[1], *seg[2:]) for seg in s_ext["segments"]]
        corr, _ = correlate.correlate(c_rep, s_rep, c_ext, s_ext)
        self.assertEqual((corr["matched_flows"], corr["flows"][0]["matched_by"]), (1, "sequence (NAT)"))
        self.assertEqual(corr["flows"][0]["missing_at_server"], 0)

    def test_no_offset_without_both_directions(self):
        offset = correlate.estimate_offset([({("c2s", 1, 1, 0, 2): [1.0]}, {("c2s", 1, 1, 0, 2): [6.01]})])
        self.assertIsNone(offset["estimated_clock_offset_ms"])
        self.assertEqual(offset["confidence"], "low")


class TaskLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        mock.patch.object(task, "PCAP_ROOT", self.root).start()
        self.db = {name: mock.patch.object(task.db, name).start() for name in
                   ("get_analysis", "claim", "set_stage", "complete", "fail", "mark_files_deleted", "fail_stale")}
        self.audit = mock.patch.object(task, "create_audit_event").start()
        self.db["claim"].return_value = True
        self.db["fail_stale"].return_value = []

    def make(self, name="tcp_normal.pcap", stored="client.pcap", mode="single"):
        analysis_id = str(uuid.uuid4())
        directory = os.path.join(self.root, analysis_id)
        os.makedirs(directory)
        shutil.copy(fixture(name), os.path.join(directory, stored))
        self.db["get_analysis"].return_value = {"id": analysis_id, "status": "queued", "mode": mode, "client_file": stored,
                                                "server_file": None, "expires_at": datetime.now(timezone.utc) + timedelta(hours=24)}
        return analysis_id, directory

    @unittest.skipUnless(HAVE_TSHARK, "tshark not installed")
    def test_success_stores_result_and_deletes_files(self):
        analysis_id, directory = self.make("tcp_retransmission.pcap")
        self.assertEqual(task.run_analysis(analysis_id)["status"], "completed")
        args = self.db["complete"].call_args.args
        self.assertEqual(args[0], analysis_id)
        self.assertEqual(args[2], 20)  # packets
        self.assertEqual(args[5], "Packet loss or reordering on the path")
        self.assertEqual([c.args[1] for c in self.db["set_stage"].call_args_list], ["validating", "extracting", "analyzing"])
        self.assertIn("performance", args[1])
        self.assertFalse(os.path.exists(directory))
        self.db["mark_files_deleted"].assert_called_once_with(analysis_id)
        self.assertEqual(self.audit.call_args.kwargs["event_type"], "pcap_analysis_completed")
        self.assertNotIn("GET /status", self.audit.call_args.kwargs["message"])

    @unittest.skipUnless(HAVE_TSHARK, "tshark not installed")
    def test_malformed_capture_fails_cleanly_and_deletes_files(self):
        analysis_id, directory = self.make("not_a_capture.pcap")
        self.assertEqual(task.run_analysis(analysis_id)["status"], "failed")
        self.assertIn("not a pcap or pcapng", self.db["fail"].call_args.args[1])
        self.assertFalse(os.path.exists(directory))
        self.assertEqual(self.audit.call_args.kwargs["event_type"], "pcap_analysis_failed")

    def test_soft_time_limit(self):
        analysis_id, directory = self.make()
        from celery.exceptions import SoftTimeLimitExceeded
        with mock.patch.object(task.analyze, "run", side_effect=SoftTimeLimitExceeded()):
            task.run_analysis(analysis_id)
        self.assertEqual(self.db["fail"].call_args.args[1], "Analysis exceeded the time limit")
        self.assertFalse(os.path.exists(directory))

    def test_unexpected_error_is_sanitized(self):
        analysis_id, _ = self.make()
        with mock.patch.object(task.analyze, "run", side_effect=KeyError("/secret/path")):
            task.run_analysis(analysis_id)
        self.assertEqual(self.db["fail"].call_args.args[1], "Internal analysis error (KeyError)")

    def test_not_queued_is_skipped_without_touching_files(self):
        analysis_id, directory = self.make()
        self.db["claim"].return_value = False
        self.assertTrue(task.run_analysis(analysis_id)["skipped"])
        self.assertTrue(os.path.exists(directory))
        self.db["fail"].assert_not_called()

    def test_tampered_stored_name_is_refused(self):
        analysis_id, directory = self.make()
        self.db["get_analysis"].return_value["client_file"] = "../../etc/passwd"
        task.run_analysis(analysis_id)
        self.assertEqual(self.db["fail"].call_args.args[1], "Unexpected stored capture name")
        self.assertFalse(os.path.exists(directory))

    def test_cleanup(self):
        expired, finished, active, orphan_old, orphan_new = (str(uuid.uuid4()) for _ in range(5))
        for name in (expired, finished, active, orphan_old, orphan_new, "not-a-uuid"):
            os.makedirs(os.path.join(self.root, name))
        old = time.time() - 7200
        os.utime(os.path.join(self.root, orphan_old), (old, old))
        now = datetime.now(timezone.utc)
        rows = {
            expired: {"status": "queued", "expires_at": now - timedelta(minutes=1)},
            finished: {"status": "completed", "expires_at": now + timedelta(hours=1)},
            active: {"status": "extracting", "expires_at": now + timedelta(hours=1)},
        }
        self.db["get_analysis"].side_effect = lambda i: rows.get(i)
        out = task.cleanup()
        self.assertEqual(sorted(out["removed_dirs"]), sorted([expired, finished, orphan_old]))
        self.assertEqual(sorted(os.listdir(self.root)), sorted([active, orphan_new, "not-a-uuid"]))

    def test_analysis_dir_is_canonical_uuid_only(self):
        with self.assertRaises(ValueError):
            task.analysis_dir("../../etc")
        self.assertEqual(os.path.dirname(task.analysis_dir(str(uuid.uuid4()))), self.root)


if __name__ == "__main__":
    unittest.main()
