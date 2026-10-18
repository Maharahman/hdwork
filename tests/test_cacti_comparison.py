import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import contextlib
import io
import json

AGENT_DIR = Path(__file__).resolve().parents[1] / "integrations/cacti-agent"
sys.path.insert(0, str(AGENT_DIR))
spec = importlib.util.spec_from_file_location("comparison", AGENT_DIR / "compare_devices.py")
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


def customer(**overrides):
    data = {"sid": "4700020-38090", "source_row": 52, "customer_name": "BANK JATIM", "status": "OK", "server": "SBS", "olt_ip": "172.23.225.48", "slot": "3", "port": "14", "onu": "1", "serial_number": "ZTEG0011"}
    data.update(overrides)
    return data


def device(**overrides):
    data = {"host_id": 8162, "server": "SBS", "description": "[HD-D] 4700020-38090_Bank Jatim Kantor Pusat", "memberships": [{"header": "BANK JATIM", "header_id": 9127, "tree_id": 34}], "detail": {"description": "[HD-D] 4700020-38090_Bank Jatim Kantor Pusat", "hostname": "172.23.225.48", "slot": "3", "port": "14", "onu_id": "1", "onu_sn": "ZTEG0011"}}
    data.update(overrides)
    return data


class ComparisonTests(unittest.TestCase):
    def status(self, source, devices):
        return comparison.compare([source], devices)["results"][0]

    def test_sid_is_full_identifier_not_substring(self):
        self.assertTrue(comparison.contains_sid("K2_ASTINET_000123_Name", "000123"))
        self.assertFalse(comparison.contains_sid("000123_Name", "123"))
        self.assertFalse(comparison.contains_sid("4700020-380900_Name", "4700020-38090"))
        self.assertFalse(comparison.contains_sid("470-123_Name", "123"))

    def test_exact_match_never_adopts_ownership(self):
        result = self.status(customer(), [device()])
        self.assertEqual(result["status"], "MATCH")
        self.assertEqual(result["ownership"], "UNREVIEWED")

    def test_external_devices_are_information_not_duplicates_or_targets(self):
        external = device(host_id=3199, description="K2 SBS BJ - 4700020-38090", memberships=[])
        result = self.status(customer(), [device(), external])
        self.assertEqual(result["status"], "MATCH")
        self.assertEqual([d["candidate_role"] for d in result["candidates"]], ["PREFIX_CANDIDATE", "EXTERNAL"])
        self.assertEqual(self.status(customer(), [external])["status"], "EXTERNAL_ONLY")
        self.assertEqual(self.status(customer(), [device(), device(server="SBU", description=external["description"])])["status"], "MATCH")

    def test_marker_is_exact_and_sid_must_follow_it(self):
        self.assertTrue(comparison.has_datin_identity("[HD-D] 000123_BANK", "000123"))
        for name in ("[HD-D] 0001234_BANK", "[HD-D] 999_SITE_000123", "prefix [HD-D] 000123_BANK", "[HD-DX] 000123_BANK", "000123_BANK"):
            self.assertFalse(comparison.has_datin_identity(name, "000123"))
        self.assertEqual(comparison.planned_description(customer(customer_detail="CAPEM")), "[HD-D] 4700020-38090_BANK JATIM_CAPEM")

    def test_prefix_does_not_bypass_protection_or_identity_change(self):
        self.assertEqual(self.status(customer(), [device(memberships=[{"header": "ATCS"}])])["status"], "PROTECTED")
        original = device()
        original["detail"]["description"] = "4700020-38090_Name"
        self.assertEqual(self.status(customer(), [original])["status"], "READ_ERROR")

    def test_location_and_header_differences_keep_before_after(self):
        result = self.status(customer(slot="4"), [device()])
        self.assertEqual(result["status"], "LOCATION_DIFF")
        self.assertEqual(result["differences"], [{"field": "slot", "spreadsheet": "4", "cacti": "3"}])
        self.assertEqual(self.status(customer(), [device(memberships=[])])["status"], "HEADER_DIFF")

    def test_other_server_and_duplicates_are_not_create_candidates(self):
        self.assertEqual(self.status(customer(), [device(server="SBU")])["status"], "OTHER_SERVER")
        self.assertEqual(self.status(customer(), [device(), device(host_id=8888)])["status"], "AMBIGUOUS")
        self.assertEqual(self.status(customer(), [device(), device(server="SBU")])["status"], "AMBIGUOUS")

    def test_atcs_is_protected_even_when_sid_matches_sheet(self):
        self.assertEqual(self.status(customer(), [device(memberships=[{"header": "ATCS"}])])["status"], "PROTECTED")
        self.assertEqual(self.status(customer(), [device(description="ATCS_4700020-38090_Simpang")])["status"], "PROTECTED")
        result = comparison.compare([], [device(description="ATCS_999_Simpang")])
        self.assertEqual(result["unmanaged"][0]["status"], "PROTECTED")

    def test_incomplete_source_and_unreadable_details_never_match(self):
        self.assertEqual(self.status(customer(slot=""), [device()])["status"], "INCOMPLETE")
        self.assertEqual(self.status(customer(olt_ip="NOT FOUND"), [device()])["status"], "INCOMPLETE")
        self.assertEqual(self.status(customer(), [device(detail=None, detail_error="timeout")])["status"], "READ_ERROR")

    def test_aliases_match_only_explicit_mapping(self):
        result = comparison.compare([customer(customer_name="BPK")], [device(memberships=[{"header": "BADAN PEMERIKSA KEUANGAN"}])], aliases={"BPK": "BADAN PEMERIKSA KEUANGAN"})
        self.assertEqual(result["results"][0]["status"], "MATCH")

    def test_zero_location_is_valid_but_empty_invalid_and_not_status_still_blocked(self):
        for field in ("slot", "port", "onu"):
            for value in (0, "0", " 0 "):
                with self.subTest(field=field, value=value):
                    source = customer(**{field: value})
                    self.assertEqual(comparison.validate_customer(source), [])
                    target = device()
                    target["detail"][comparison.FIELDS[field]] = "0"
                    self.assertEqual(self.status(source, [target])["status"], "MATCH")
            for value in (None, "", " ", -1, "-1", "1.5", "abc"):
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.status(customer(**{field: value}), [device()])["status"], "INCOMPLETE")
        self.assertEqual(self.status(customer(slot=0, port=0, onu=0, status="NOT"), [device()])["status"], "INCOMPLETE")
        self.assertEqual(comparison.canonical_number(0), "0")
        self.assertEqual(comparison.safe_csv(0), "0")

    def test_conflicts_remain_held_and_are_not_labeled_unmapped(self):
        result = comparison.compare([], [device()], source_conflicts=[{"sid": "4700020-38090", "rows": [2, 3]}])
        self.assertEqual(result["results"][0]["status"], "SOURCE_CONFLICT")
        self.assertEqual(result["unmanaged"], [])

    def test_incomplete_device_list_aborts_instead_of_false_missing(self):
        html = '''<div>Showing Rows 1 to 1 of 2</div><table><tr><td><a href="host.php?action=edit&id=8162">4700020-38090_Name</a></td></tr></table>'''
        with self.assertRaisesRegex(RuntimeError, "belum lengkap"):
            comparison.parse_device_list(html)
        devices = comparison.parse_device_list(html.replace("of 2", "of 1"))
        self.assertEqual(devices[0]["host_id"], 8162)

    def test_all_rows_requires_independent_total_and_checks_completeness(self):
        html = '<div>Showing All Rows</div><a href="host.php?action=edit&id=8162">4700020-38090_Name</a>'
        with self.assertRaisesRegex(RuntimeError, "Jumlah total"):
            comparison.parse_device_list(html)
        self.assertEqual(len(comparison.parse_device_list(html, expected_total=1)), 1)
        with self.assertRaisesRegex(RuntimeError, "belum lengkap"):
            comparison.parse_device_list(html, expected_total=2)
        # Repeated links for one host must not inflate the inventory count.
        with self.assertRaisesRegex(RuntimeError, "belum lengkap"):
            comparison.parse_device_list(html + html, expected_total=2)

    def test_paginated_total_and_missing_or_changed_counts(self):
        self.assertEqual(comparison.parse_device_total('<div>Showing Rows 1 to 10 of 4,099</div>'), 4099)
        with self.assertRaisesRegex(RuntimeError, "Jumlah total"):
            comparison.parse_device_list('<div>Unknown listing</div>', expected_total=0)
        with self.assertRaisesRegex(RuntimeError, "berubah"):
            comparison.parse_device_list('<div>Showing Rows 1 to 1 of 1</div>', expected_total=2)

    def test_host_form_reads_only_comparison_fields(self):
        html = '<input name="id" value="8162">' + ''.join(f'<input name="{name}" value="{value}">' for name, value in device()["detail"].items())
        self.assertEqual(comparison.parse_device_detail(html, 8162)["slot"], "3")
        with self.assertRaisesRegex(RuntimeError, "Host ID"):
            comparison.parse_device_detail(html, 8163)

    def test_tree_membership_reads_id_and_header(self):
        html = '''<table><tr><td><a href="tree.php?action=item_edit&tree_id=34&id=9127">BANK JATIM</a></td><td>Heading</td></tr>
        <tr><td><a href="host.php?action=edit&id=8162">(Edit host)</a></td><td>Host</td></tr></table>'''
        result = comparison.parse_tree_memberships(html, 34)
        self.assertEqual(result[8162][0]["header_id"], 9127)

    def test_report_exports_source_and_cacti_values_without_formula_execution(self):
        result = comparison.compare([customer(slot="4", customer_detail="=SENSITIVE()")], [device()])
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "comparison.report"
            comparison.write_reports(prefix, result)
            text = Path(str(prefix) + ".csv").read_text(encoding="utf-8-sig")
            self.assertIn("sheet_slot,cacti_slot", text)
            self.assertIn("'=SENSITIVE()", text)
            self.assertTrue(Path(str(prefix) + ".json").exists())

    def test_plan_cli_uses_only_reads_and_holds_when_mapping_changes(self):
        source = {"spreadsheet_id": "sheet", "snapshot_at": "2026-10-19T00:00:00Z", "customers": [customer(slot="0")], "conflicts": [], "aliases": []}
        for changed in (False, True):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                prefix = Path(directory) / "plan.report"
                api = Mock()
                mapping_reads = []
                def request(method, path):
                    self.assertEqual(method, "GET")
                    if path == "/api/datin-cacti/device-source":
                        return source
                    self.assertEqual(path, "/api/datin-cacti/device-mappings")
                    mapping_reads.append(path)
                    return {"spreadsheet_id": "sheet", "snapshot_at": source["snapshot_at"], "mappings": [dict(sid="other", server="SBS", host_id=7)] if changed and len(mapping_reads) > 1 else []}
                api.request.side_effect = request
                clients = []
                def client(server):
                    c = Mock(server=server)
                    c.headers.return_value = {"BANK JATIM": [{"header_id": 9127}]}
                    clients.append(c)
                    return c
                with patch.object(comparison, "HDWork", return_value=api), patch.object(comparison, "Cacti", side_effect=client), patch.object(comparison, "fetch_inventory", return_value=[]), patch.object(comparison, "load_dotenv"), patch.object(sys, "argv", ["compare", "--plan", "--report-prefix", str(prefix)]), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = comparison.main()
                self.assertEqual(code, 2 if changed else 0)
                report = json.loads(Path(str(prefix) + ".json").read_text())
                actions = report["device_plan"]["rows"][0]["actions"]
                self.assertEqual(actions, ["HOLD"] if changed else ["CREATE_DEVICE", "ATTACH_TO_TREE"])
                self.assertIn("planned_actions", Path(str(prefix) + ".csv").read_text())
                for c in clients:
                    c.session.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
