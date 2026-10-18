import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations/cacti-agent"))
from device_plan import build_device_plan
from sync_headers import SERVERS


class DevicePlanTests(unittest.TestCase):
    def setUp(self):
        self.row = {"sid": "000123", "source_row": 2, "institution": "BANK JATIM", "expected_server": "SBS", "status": "NOT_FOUND", "reasons": [], "differences": [], "candidates": [], "spreadsheet": {"slot": "0", "port": "0", "onu": "0"}, "planned_description": "[HD-D] 000123_BANK JATIM_CAPEM"}
        self.headers = {"SBS": {"BANK JATIM": [{"header_id": 9127}]}}
        self.mapping = {"sid": "000123", "server": "SBS", "tree_id": 34, "host_id": 42, "header_id": 9127, "ownership_status": "VERIFIED", "verified_at": "2026-10-19"}
        self.host = {"server": "SBS", "host_id": 42, "candidate_role": "PREFIX_CANDIDATE", "description": self.row["planned_description"], "detail": {"description": self.row["planned_description"]}, "memberships": [{"header_id": 9127}]}

    def plan(self, mappings=None, inventory=None, headers=None, stale=False):
        return build_device_plan({"results": [self.row], "source_changed_during_scan": stale}, mappings or [], self.headers if headers is None else headers, inventory or [], SERVERS)

    def mapped(self):
        self.row.update(status="MATCH", candidates=[self.host])
        return self.plan([self.mapping], [self.host])["rows"][0]

    def test_new_and_external_preview_never_modify_external_hosts(self):
        self.row.update(status="EXTERNAL_ONLY", candidates=[dict(self.host, candidate_role="EXTERNAL")])
        before = copy.deepcopy(self.row)
        result = self.plan()
        row = result["rows"][0]
        self.assertEqual(row["actions"], ["CREATE_DEVICE", "ATTACH_TO_TREE"])
        self.assertEqual(row["desired"]["slot"], "0")
        self.assertNotIn("host_id", row)
        self.assertEqual(row["external_devices"], [{"server": "SBS", "host_id": 42}])
        self.assertFalse(result["executable"])
        self.assertFalse(row["approved"])
        self.assertEqual(self.row, before)

    def test_missing_header_is_dependency_and_duplicates_hold(self):
        self.assertEqual(self.plan(headers={})["rows"][0]["actions"], ["CREATE_HEADER", "CREATE_DEVICE", "ATTACH_TO_TREE"])
        self.headers["SBS"]["BANK JATIM"].append({"header_id": 7})
        self.assertEqual(self.plan()["rows"][0]["actions"], ["HOLD"])

    def test_conflict_invalid_protected_stale_and_unmapped_prefix_hold(self):
        for status in ("SOURCE_CONFLICT", "INCOMPLETE", "AMBIGUOUS", "PROTECTED", "READ_ERROR", "OTHER_SERVER"):
            self.row["status"] = status
            self.assertEqual(self.plan()["rows"][0]["actions"], ["HOLD"])
        self.row.update(status="NOT_FOUND", candidates=[])
        self.assertEqual(self.plan(stale=True)["rows"][0]["actions"], ["HOLD"])
        self.row.update(status="MATCH", candidates=[self.host])
        self.assertEqual(self.plan()["rows"][0]["actions"], ["HOLD"])

    def test_verified_mapping_rechecks_host_identity_and_server(self):
        self.assertEqual(self.mapped()["actions"], ["NO_CHANGE"])
        self.assertEqual(self.plan([self.mapping], [])["rows"][0]["actions"], ["HOLD"])
        self.row["candidates"] = []
        self.assertEqual(self.plan([self.mapping], [self.host])["rows"][0]["actions"], ["HOLD"])
        self.row["candidates"] = [self.host]
        for field, value in (("server", "SBU"), ("tree_id", 28), ("ownership_status", "REVIEW_REQUIRED"), ("verified_at", None)):
            self.assertEqual(self.plan([{**self.mapping, field: value}], [self.host])["rows"][0]["actions"], ["HOLD"])

    def test_update_attach_and_wrong_membership_hold(self):
        self.mapped()
        self.row["differences"] = [{"field": "slot", "spreadsheet": "0", "cacti": "3"}]
        self.assertEqual(self.plan([self.mapping], [self.host])["rows"][0]["actions"], ["UPDATE_DEVICE"])
        self.host["memberships"] = []
        self.assertEqual(self.plan([self.mapping], [self.host])["rows"][0]["actions"], ["UPDATE_DEVICE", "ATTACH_TO_TREE"])
        self.host["memberships"] = [{"header_id": 99}]
        self.assertEqual(self.plan([self.mapping], [self.host])["rows"][0]["actions"], ["HOLD"])


if __name__ == "__main__":
    unittest.main()
