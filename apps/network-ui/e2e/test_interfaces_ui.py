"""
Browser tests for Device Detail > Interfaces against the production build and the local
mock API (e2e/mock_api.py; synthetic data, no device). Checks the one-request initial load,
lazy history, filters/search/sort, the drawer, freshness labels and failure isolation.

Run from apps/network-ui after `VITE_API_BASE_URL= npx vite build`:
    docker run --rm --network host -v $PWD:/ui -w /ui mcr.microsoft.com/playwright/python:v1.49.1-noble \
        sh -c "pip install -q playwright==1.49.1 && python -m unittest e2e.test_interfaces_ui -v"
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
PORT = int(os.getenv("E2E_IFACE_PORT", "58096"))
BASE = f"http://127.0.0.1:{PORT}"
TAB = f"{BASE}/devices/12?tab=interfaces"


class InterfacesUiTests(unittest.TestCase):
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

    def gets(self):
        return json.loads(urllib.request.urlopen(f"{BASE}/__gets").read())

    def setUp(self):
        self.control("/__reset")
        self.addCleanup(self.control, "/__reset")
        self.page = self.browser.new_page(viewport={"width": 1600, "height": 1200})
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.addCleanup(self.page.close)

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def open_tab(self, mode="fresh"):
        self.control("/__iface_mode", {"mode": mode})
        self.page.goto(TAB)
        if mode in ("fresh", "stale", "partial"):
            expect(self.page.locator(".iface-table tbody tr")).to_have_count(7, timeout=10000)

    def rows(self):
        return self.page.locator(".iface-table tbody tr")

    def names(self):
        return [n.strip() for n in self.page.locator(".iface-table tbody tr td.cell-primary .mono").all_inner_texts()]

    # ---- initial load ------------------------------------------------------------------------
    def test_one_snapshot_request_and_no_history_until_graphs(self):
        self.open_tab()
        self.page.wait_for_timeout(800)
        gets = self.gets()
        self.assertEqual(gets.count("/api/v1/devices/12/interfaces"), 1)       # one request draws the tab
        self.assertFalse([g for g in gets if "/interfaces/" in g])             # no per-interface calls
        self.assertNotIn("/api/v1/devices/12/metrics", gets)                   # CPU/memory history not loaded here
        self.page.get_by_role("button", name="View details of Eth 1/1/25", exact=True).click()
        self.page.wait_for_timeout(300)
        self.assertFalse([g for g in self.gets() if g.endswith("/metrics") and "/interfaces/" in g])  # Overview tab only
        self.page.get_by_role("tab", name="Graphs").click()
        expect(self.page.locator(".drawer-chart")).to_have_count(5, timeout=10000)
        history = [g for g in self.gets() if "/interfaces/" in g and g.endswith("/metrics")]
        self.assertEqual(history, ["/api/v1/devices/12/interfaces/5/metrics"])  # only the selected interface

    def test_summary_cards_charts_and_table(self):
        self.open_tab()
        kpis = self.page.locator(".iface-kpis .kpi-card")
        expect(kpis).to_have_count(6)
        values = dict(zip([l.lower() for l in kpis.locator(".kpi-label").all_inner_texts()],
                          kpis.locator(".kpi-value").all_inner_texts()))
        self.assertEqual(values, {"total interfaces": "7", "up": "5", "down": "1", "erroring": "1", "trunks": "3",
                                  "access ports": "4"})
        util = self.page.locator("section.widget", has_text="Interface Utilization").locator(".bar-row")
        expect(util).to_have_count(4)                       # only interfaces with a real value (no padding to 5)
        expect(util.first).to_contain_text("Eth 1/1/26")
        expect(util.first).to_contain_text("95.5%")
        errors = self.page.locator("section.widget", has_text="Interface Errors / Discards").locator("tbody tr")
        expect(errors).to_have_count(1)
        expect(errors.first).to_contain_text("Eth 1/1/25")
        legend = self.page.locator("section.widget", has_text="Interface Status Distribution").locator(".donut-legend li")
        expect(legend).to_have_count(4)
        row = self.rows().filter(has_text="Eth 1/1/3")
        expect(row.locator(".badge")).to_contain_text("Down")
        expect(row.locator("td").nth(6)).to_have_text("—")  # unknown utilization is "—", never 0%
        expect(self.rows().filter(has_text="Eth 1/1/25").locator(".iface-role")).to_have_text("inter-switch")
        expect(self.page.locator(".iface-freshness")).to_contain_text("Last interface poll")

    # ---- filters / search / sort ------------------------------------------------------------
    def test_filters_search_and_sorting(self):
        self.open_tab()
        chips = self.page.get_by_role("group", name="Filter interfaces")
        chips.get_by_role("button", name="Down").first.click()
        self.assertEqual(self.names(), ["Eth 1/1/3"])
        chips.get_by_role("button", name="Admin Down").click()
        self.assertEqual(self.names(), ["Eth 1/1/4"])
        chips.get_by_role("button", name="Trunk").click()
        self.assertEqual(self.names(), ["Eth 1/1/25", "Eth 1/1/26", "Port-channel 10"])
        chips.get_by_role("button", name="Erroring").click()
        self.assertEqual(self.names(), ["Eth 1/1/25"])
        chips.get_by_role("button", name="All").click()
        search = self.page.get_by_label("Search interfaces by name, description or VLAN")
        search.fill("printer")
        self.assertEqual(self.names(), ["Eth 1/1/2"])
        search.fill("20")  # VLAN 20 (access) and trunks allowing 10-20
        self.assertIn("Eth 1/1/2", self.names())
        search.fill("")
        rx_header = self.page.locator("th", has=self.page.get_by_role("button", name="RX Util"))
        rx_header.get_by_role("button").click()
        expect(rx_header).to_have_attribute("aria-sort", "descending")
        self.assertEqual(self.names()[:3], ["Eth 1/1/26", "Eth 1/1/25", "Eth 1/1/1"])
        self.assertEqual(self.names()[-3:], ["Eth 1/1/3", "Eth 1/1/4", "Port-channel 10"])  # unknown last
        rx_header.get_by_role("button").click()
        expect(rx_header).to_have_attribute("aria-sort", "ascending")
        self.assertEqual(self.names()[0], "Eth 1/1/2")

    # ---- drawer ------------------------------------------------------------------------------
    def test_drawer_tabs_keyboard_and_focus_return(self):
        self.open_tab()
        view = self.page.get_by_role("button", name="View details of Eth 1/1/1", exact=True)
        view.click()
        drawer = self.page.get_by_role("dialog")
        expect(drawer).to_contain_text("SERVER-A")
        expect(self.page.get_by_role("button", name="Close interface details")).to_be_focused()
        expect(drawer.locator(".detail-list")).to_contain_text("Access VLAN")
        drawer.get_by_role("tab", name="Counters").click()
        expect(drawer).to_contain_text("812,345,678,901")
        drawer.get_by_role("tab", name="Graphs").click()
        expect(drawer).to_contain_text("Historical interface data not yet available", timeout=10000)
        self.page.keyboard.press("Escape")
        expect(self.page.get_by_role("dialog")).to_have_count(0)
        expect(view).to_be_focused()  # focus returns to the row's View button

    def test_switching_interfaces_and_ranges_cancels_obsolete_requests(self):
        self.open_tab()
        self.page.get_by_role("button", name="View details of Eth 1/1/25", exact=True).click()
        drawer = self.page.get_by_role("dialog")
        drawer.get_by_role("tab", name="Graphs").click()
        expect(drawer.locator(".drawer-chart")).to_have_count(5, timeout=10000)
        drawer.get_by_role("button", name="7D").click()
        expect(drawer.get_by_role("button", name="7D")).to_have_attribute("aria-pressed", "true")
        expect(drawer.locator(".drawer-chart")).to_have_count(5)
        self.page.get_by_role("button", name="View details of Eth 1/1/2", exact=True).click()
        expect(self.page.get_by_role("dialog")).to_contain_text("PRINTER")
        expect(self.page.get_by_role("dialog")).not_to_contain_text("SERVER-A")

    # ---- freshness / empty / partial / failure ------------------------------------------------
    def test_stale_snapshot_is_labelled_stale_not_down(self):
        self.open_tab("stale")
        expect(self.page.locator(".iface-freshness")).to_contain_text("Stale")
        expect(self.page.locator(".iface-freshness")).to_contain_text("Last successful poll")
        expect(self.page.locator(".banner, .notice-banner").filter(has_text="Interface data is stale")).to_contain_text(
            "Device unreachable (TCP 22)")
        expect(self.rows().filter(has_text="Eth 1/1/1").locator(".badge")).to_contain_text("Up")

    def test_not_collected_shows_empty_state(self):
        self.open_tab("empty")
        expect(self.page.get_by_text("No interface data collected yet.")).to_be_visible(timeout=10000)
        expect(self.page.get_by_text("a configuration change was in progress")).to_be_visible()
        expect(self.page.locator(".iface-kpis")).to_have_count(0)

    def test_partial_collection_notice(self):
        self.open_tab("partial")
        expect(self.page.get_by_text("Partial collection: some fields are unavailable.")).to_be_visible()

    def test_api_failure_is_isolated_to_the_tab(self):
        self.open_tab("error")
        expect(self.page.get_by_text("Interface data could not be loaded.")).to_be_visible(timeout=10000)
        expect(self.page.locator(".detail-title h1")).to_have_text("Kenda-Core-1")  # rest of the page still works
        self.page.get_by_role("tab", name="Overview").click()
        expect(self.page.locator(".detail-kpis")).to_be_visible()

    def test_render_time_for_a_52_port_switch(self):
        self.control("/__iface_mode", {"mode": "large"})
        self.page.goto(f"{BASE}/devices/12")
        expect(self.page.locator(".detail-title h1")).to_have_text("Kenda-Core-1")
        before = len(self.gets())
        started = time.perf_counter()
        self.page.get_by_role("tab", name="Interfaces").click()
        expect(self.rows()).to_have_count(52, timeout=10000)
        elapsed = (time.perf_counter() - started) * 1000
        self.page.wait_for_timeout(500)
        new = self.gets()[before:]
        self.assertEqual([g for g in new if "interfaces" in g], ["/api/v1/devices/12/interfaces"])  # one request
        t0 = time.perf_counter()
        self.page.get_by_role("group", name="Filter interfaces").get_by_role("button", name="Trunk").click()
        expect(self.rows()).to_have_count(13)
        filter_ms = (time.perf_counter() - t0) * 1000
        print(f"\n    Interfaces tab, 52 interfaces: click -> table rendered {elapsed:.0f} ms (incl. API round trip); "
              f"filter re-render {filter_ms:.0f} ms")
        self.assertLess(elapsed, 3000)

    def test_layout_without_horizontal_overflow(self):
        for width in (1600, 1024):
            self.page.set_viewport_size({"width": width, "height": 1000})
            self.open_tab()
            overflow = self.page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
            self.assertFalse(overflow, width)


if __name__ == "__main__":
    unittest.main()
