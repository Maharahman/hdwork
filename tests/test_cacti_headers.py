import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock

import requests

spec = importlib.util.spec_from_file_location("headers_agent", Path(__file__).resolve().parents[1] / "integrations/cacti-agent/sync_headers.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

FORM = '''<form action="tree.php" method="post">
<input type="hidden" name="__csrf_magic" value="test-token">
<input type="hidden" name="graph_tree_id" value="34">
<input type="hidden" name="type" value="1">
<input type="hidden" name="action" value="save">
<select name="parent_item_id"><option value="0" selected>[root]</option></select>
<select name="type_select"><option value="header" selected>Header</option></select>
<input name="title"><select name="sort_children_type"><option value="1" selected>Manual</option></select>
</form>'''


class HeaderAgentTests(unittest.TestCase):
    def client(self):
        session = Mock()
        response = Mock(text=FORM, url="http://10.96.33.156/ont/tree.php")
        session.get.return_value = response
        session.post.return_value = response
        client = agent.Cacti("SBS", session)
        client.headers = Mock()
        return client, session

    def test_parse_headers_ignores_hosts_and_other_trees(self):
        html = '''<table><tr><td><a href="tree.php?action=item_edit&tree_id=34&id=9127"> BANK JATIM </a>
        <a href="tree.php?action=item_edit&tree_id=34&parent_id=9127">Add</a></td><td>Heading</td></tr>
        <tr><td><a href="tree.php?action=item_edit&tree_id=34&id=42">Host: test</a></td><td>Host</td></tr>
        <tr><td><a href="tree.php?action=item_edit&tree_id=28&id=5">Wrong server tree</a></td><td>Heading</td></tr></table>'''
        headers = agent.parse_headers(html, "http://10.96.33.156/ont", 34)
        self.assertEqual(headers, {"BANK JATIM": [{"name": "BANK JATIM", "header_id": 9127}]})

    def test_preview_and_existing_never_post(self):
        client, session = self.client()
        client.headers.return_value = {}
        self.assertEqual(client.ensure_header("BANK MANDIRI")["action"], "would_create")
        client.headers.return_value = {"BANK JATIM": [{"header_id": 9127}]}
        self.assertEqual(client.ensure_header("BANK JATIM", apply=True)["header_id"], 9127)
        session.post.assert_not_called()

    def test_create_preserves_csrf_and_reads_actual_id(self):
        client, session = self.client()
        client.headers.side_effect = [{}, {"BANK MANDIRI": [{"header_id": 9527}]}]
        result = client.ensure_header("BANK MANDIRI", apply=True)
        self.assertEqual(result["header_id"], 9527)
        payload = session.post.call_args.kwargs["data"]
        self.assertEqual(payload["__csrf_magic"], "test-token")
        self.assertEqual(payload["parent_item_id"], "0")
        self.assertEqual(payload["title"], "BANK MANDIRI")
        self.assertEqual(payload["graph_tree_id"], "34")

    def test_timeout_does_not_repeat_create(self):
        client, session = self.client()
        client.headers.side_effect = [{}, {"BANK MANDIRI": [{"header_id": 9527}]}]
        session.post.side_effect = requests.Timeout()
        self.assertEqual(client.ensure_header("BANK MANDIRI", apply=True)["action"], "verified_after_timeout")
        self.assertEqual(session.post.call_count, 1)

    def test_header_create_keeps_action_outside_legacy_form(self):
        client, session = self.client()
        html = FORM.replace('<input type="hidden" name="action" value="save">', '') + '<input type="hidden" name="action" value="save">'
        session.get.return_value.text = html
        client.headers.side_effect = [{}, {"BANK MANDIRI": [{"header_id": 9527}]}]
        client.ensure_header("BANK MANDIRI", apply=True)
        self.assertEqual(session.post.call_args.kwargs['data']['action'], 'save')
        self.assertEqual(session.post.call_args.kwargs['data']['__csrf_magic'], 'test-token')

    def test_missing_or_duplicate_action_blocks_header_post(self):
        for html in (FORM.replace('<input type="hidden" name="action" value="save">', ''), FORM + '<input name="action" value="save">'):
            client, session = self.client()
            session.get.return_value.text = html
            client.headers.return_value = {}
            with self.assertRaisesRegex(RuntimeError, 'action'):
                client.ensure_header("BANK MANDIRI", apply=True)
            session.post.assert_not_called()

    def test_duplicate_headers_block_writes(self):
        client, session = self.client()
        client.headers.return_value = {"BANK JATIM": [{"header_id": 1}, {"header_id": 2}]}
        with self.assertRaisesRegex(RuntimeError, "duplikat"):
            client.ensure_header("BANK JATIM", apply=True)
        session.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
