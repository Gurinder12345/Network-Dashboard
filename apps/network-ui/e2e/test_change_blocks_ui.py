"""
Browser tests for the multi-block change UI (Changes + Approvals pages) against the
production build and a local mock API (e2e/mock_api.py; synthetic data, no device).

Run from apps/network-ui after `VITE_API_BASE_URL= npx vite build`:
    docker run --rm --network host -v $PWD:/ui -w /ui mcr.microsoft.com/playwright/python:v1.49.1-noble \
        sh -c "pip install -q playwright==1.49.1 && python -m unittest e2e.test_change_blocks_ui -v"
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
PORT = int(os.getenv("E2E_PORT", "58095"))
BASE = f"http://127.0.0.1:{PORT}"


class ChangeBlocksUiTests(unittest.TestCase):
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

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1600, "height": 1200})
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.addCleanup(self.page.close)

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def requests(self):
        return json.loads(urllib.request.urlopen(f"{BASE}/__requests").read())

    def preview(self):
        return self.page.locator(".change-form .cli-preview pre").inner_text()

    def fill_command(self, block, n, text):
        self.page.get_by_label(f"Block {block} command {n}", exact=True).fill(text)

    # ---- editor ------------------------------------------------------------------------------
    def build_change(self):
        page = self.page
        page.goto(f"{BASE}/changes")
        page.select_option("select[aria-label='Target device']", "12")
        page.get_by_label("Block 1 parent").fill("interface ethernet1/1/18")
        self.fill_command(1, 1, "description APP-SERVER")
        page.get_by_label("Block 1 command 1", exact=True).press("Enter")  # Enter adds the next command row
        self.fill_command(1, 2, "switchport mode trunk")
        page.get_by_role("button", name="+ Add configuration block").click()
        page.get_by_label("Block 2 parent").fill("router ospf 1")
        self.fill_command(2, 1, "router-id 10.0.0.1")
        page.get_by_role("button", name="+ Add configuration block").click()
        self.fill_command(3, 1, "ip routing")  # empty parent -> global block

    def test_add_blocks_commands_global_and_preview(self):
        self.build_change()
        expect(self.page.locator(".block-editor").nth(2).locator(".global-tag")).to_have_text("GLOBAL CONFIGURATION")
        self.assertEqual(self.preview(), "! Block 1\ninterface ethernet1/1/18\n description APP-SERVER\n switchport mode trunk\n\n"
                                         "! Block 2\nrouter ospf 1\n router-id 10.0.0.1\n\n! Block 3 - Global\nip routing")
        expect(self.page.locator(".panel-header .count-tag").first).to_have_text("3 block(s) · 4 command(s)")

    def test_move_duplicate_and_delete(self):
        self.build_change()
        page = self.page
        page.get_by_role("button", name="Move block 3 up").click()        # global block becomes block 2
        self.assertTrue(self.preview().split("\n\n")[1].startswith("! Block 2 - Global\nip routing"))
        page.get_by_role("button", name="Move block 1 down").click()      # 1/1/18 block becomes block 2
        self.assertTrue(self.preview().startswith("! Block 1 - Global\nip routing\n\n! Block 2\ninterface ethernet1/1/18"))
        page.locator(".block-editor").nth(1).get_by_role("button", name="Duplicate").click()
        self.assertEqual(self.preview().count("interface ethernet1/1/18"), 2)
        page.get_by_role("button", name="Delete block 2 command 1").click()
        self.assertIn("! Block 2\ninterface ethernet1/1/18\n switchport mode trunk\n\n! Block 3\ninterface ethernet1/1/18\n description",
                      self.preview())
        page.locator(".block-editor").nth(2).get_by_role("button", name="Delete", exact=True).click()
        self.assertEqual(self.preview().count("! Block"), 3)
        expect(page.get_by_role("button", name="Move block 1 up")).to_be_disabled()

    def test_paste_splits_lines_into_commands(self):
        page = self.page
        page.goto(f"{BASE}/changes")
        page.get_by_label("Block 1 parent").fill("interface Tw1/0/3")
        field = page.get_by_label("Block 1 command 1", exact=True)
        field.focus()
        page.evaluate("""() => {
            const el = document.activeElement;
            const data = new DataTransfer();
            data.setData('text', 'description A\\nshutdown\\n\\nno shutdown');
            el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: data, bubbles: true, cancelable: true}));
        }""")
        self.assertEqual(self.preview(), "! Block 1\ninterface Tw1/0/3\n description A\n shutdown\n no shutdown")

    # ---- submit + precheck rendering --------------------------------------------------------
    def test_submit_sends_ordered_blocks_and_renders_precheck(self):
        self.build_change()
        page = self.page
        page.locator("textarea").fill("show vlan\nshow running-configuration interface ethernet1/1/18")
        before = len(self.requests())
        page.get_by_role("button", name="Run precheck").click()
        expect(page.locator(".precheck-block")).to_have_count(3, timeout=10000)
        body = self.requests()[before]
        self.assertEqual(body, {"device_id": 12, "blocks": [
            {"parent": "interface ethernet1/1/18", "commands": ["description APP-SERVER", "switchport mode trunk"]},
            {"parent": "router ospf 1", "commands": ["router-id 10.0.0.1"]},
            {"parent": None, "commands": ["ip routing"]}],
            "verification_commands": ["show vlan", "show running-configuration interface ethernet1/1/18"]})
        results = page.locator(".precheck-results")
        expect(results.locator(".check-item")).to_have_count(5)
        expect(results).to_contain_text("Semantic verification")
        expect(results).to_contain_text("PARTIAL")
        expect(results.locator(".precheck-block").nth(1)).to_contain_text("Not available")
        expect(results.locator(".precheck-block").nth(1)).to_contain_text("semantic pre-validation is not available")
        expect(results).to_contain_text("One pending approval")
        expect(page.locator(".panel-header .badge").last).to_contain_text("approval pending")

    def test_structural_errors_block_submission(self):
        page = self.page
        page.goto(f"{BASE}/changes")
        page.select_option("select[aria-label='Target device']", "1")
        self.fill_command(1, 1, "end")
        before = len(self.requests())
        page.get_by_role("button", name="Run precheck").click()
        expect(page.locator(".precheck-results")).to_contain_text("configure / end / exit / do are handled by the platform")
        expect(page.locator(".precheck-results")).to_contain_text("Not submitted")
        self.assertEqual(len(self.requests()), before)

    def test_no_command_category_is_blocked_in_the_browser(self):
        page = self.page
        page.goto(f"{BASE}/changes")
        page.select_option("select[aria-label='Target device']", "12")
        self.fill_command(1, 1, "router bgp 65000")
        page.get_by_label("Block 1 command 1", exact=True).press("Enter")
        self.fill_command(1, 2, "aaa authentication login default local")
        before = len(self.requests())
        page.get_by_role("button", name="Run precheck").click()
        expect(page.locator(".precheck-block").first).to_be_visible(timeout=10000)
        self.assertEqual(len(self.requests()), before + 1)

    # ---- approvals ---------------------------------------------------------------------------
    def test_approval_review_shows_blocks_preview_and_precheck(self):
        page = self.page
        page.goto(f"{BASE}/approvals")
        row = page.locator("tbody tr").first
        expect(row).to_contain_text("3 block(s) · 4 command(s)")
        row.get_by_role("button", name="Approve").click()
        dialog = page.get_by_role("dialog")
        expect(dialog.locator(".change-counts")).to_contain_text("3 block(s)")
        expect(dialog.locator(".blocks-view-block")).to_have_count(3)
        expect(dialog.locator(".global-tag")).to_have_text("GLOBAL CONFIGURATION")
        expect(dialog.locator(".cli-preview pre")).to_have_text(
            "! Block 1\ninterface ethernet1/1/18\n description APP-SERVER\n switchport mode trunk\n\n"
            "! Block 2\ninterface ethernet1/1/19\n switchport access vlan 50\n\n! Block 3 - Global\nip routing")
        expect(dialog.locator(".check-item")).to_have_count(5)
        expect(dialog).to_contain_text("Verification commands after apply")
        expect(dialog).to_contain_text("one approval covers the whole change")

    def test_partial_apply_is_displayed_block_by_block(self):
        page = self.page
        page.goto(f"{BASE}/approvals")
        row = page.locator("tbody tr").nth(1)
        expect(row).to_contain_text("Partial apply")
        execution = row.locator(".execution-view")
        expect(execution).to_be_visible()  # opened by default for a partial apply
        expect(execution.locator(".execution-block")).to_have_count(3)
        expect(execution.locator(".execution-block").nth(0)).to_contain_text("applied")
        expect(execution.locator(".execution-block").nth(1)).to_contain_text("Command 1 rejected: switchport access vlan 50")
        expect(execution.locator(".execution-block").nth(1)).to_contain_text("% Error: VLAN 50 does not exist.")
        expect(execution.locator(".execution-block").nth(2)).to_contain_text("not attempted")
        expect(execution).to_contain_text("applied blocks were not rolled back")

    def test_historical_single_parent_change_displays_as_one_block(self):
        page = self.page
        page.goto(f"{BASE}/approvals")
        row = page.locator("tbody tr").nth(2)
        expect(row).to_contain_text("1 block(s) · 1 command(s)")
        row.locator("summary", has_text="CLI preview").click()
        expect(row.locator(".config-preview")).to_have_text("! Block 1\ninterface Tw1/0/3\n description OLD-CHANGE")


if __name__ == "__main__":
    unittest.main()
