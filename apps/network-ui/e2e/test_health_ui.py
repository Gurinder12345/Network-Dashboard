"""
Health reason / evidence display (Devices, Overview, Device Detail) against the production
build and the local mock API. The mock switches Kenda-Core-1 to "answers ping, no SSH".

    docker run --rm --network host -v $PWD:/ui -w /ui mcr.microsoft.com/playwright/python:v1.49.1-noble \
        sh -c "pip install -q playwright==1.49.1 && python -m unittest e2e.test_health_ui -v"
"""

import json
import os
import subprocess
import sys
import time
import unittest
import urllib.request

from playwright.sync_api import expect, sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(os.path.dirname(HERE), "dist")
PORT = int(os.getenv("E2E_HEALTH_PORT", "58098"))
BASE = f"http://127.0.0.1:{PORT}"


class HealthUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_api.py"), DIST, str(PORT)])
        for _ in range(50):
            try:
                urllib.request.urlopen(f"{BASE}/api/v1/devices", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.terminate()
        cls.server.wait(timeout=10)

    def control(self, path, body=None):
        request = urllib.request.Request(f"{BASE}{path}", data=json.dumps(body or {}).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        urllib.request.urlopen(request).read()

    def setUp(self):
        self.control("/__reset")
        self.control("/__health_mode", {"mode": "degraded"})
        self.addCleanup(self.control, "/__reset")
        self.page = self.browser.new_page(viewport={"width": 1600, "height": 1200})
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.addCleanup(self.page.close)

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def test_devices_list_shows_degraded_with_reason_not_down(self):
        self.page.goto(f"{BASE}/devices")
        row = self.page.locator("tbody tr", has_text="Kenda-Core-1")
        expect(row.locator(".badge").first).to_contain_text("degraded")
        expect(row).not_to_contain_text("down")
        expect(row.locator("summary")).to_have_text("SSH management unavailable")
        self.assertIn("ICMP ok · TCP/22 fail · SSH fail · CLI fail", row.locator("td").nth(4).get_attribute("title"))

    def test_overview_status_table_shows_reason(self):
        self.page.goto(f"{BASE}/")
        row = self.page.locator(".panel", has_text="Device Status").locator("tbody tr", has_text="Kenda-Core-1")
        expect(row).to_contain_text("SSH management unavailable", timeout=10000)

    def test_device_detail_evidence_rows(self):
        self.page.goto(f"{BASE}/devices/12")
        panel = self.page.locator(".detail-health")
        expect(panel).to_contain_text("Degraded", ignore_case=True, timeout=10000)
        rows = dict(zip(panel.locator("dt").all_inner_texts(), panel.locator("dd").all_inner_texts()))
        rows = {k.strip().lower(): v.strip() for k, v in rows.items()}
        self.assertEqual(rows["reason"], "SSH management unavailable")
        self.assertEqual(rows["reachability (icmp)"], "Reachable")
        self.assertEqual(rows["tcp/22"], "Unreachable")
        self.assertEqual(rows["ssh"], "Unavailable")
        self.assertEqual(rows["cli"], "Unavailable")


if __name__ == "__main__":
    unittest.main()
