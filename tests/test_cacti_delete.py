import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integrations/cacti-agent'))
import delete_device as delete
from test_cacti_edit import live


def plan():
    p = {'version': 1, 'target': delete.TARGET, 'spreadsheet_id': 'sheet', 'evidence': 'HUMAN_CONFIRMED_NOT',
         'before': live()['detail'], 'option_guard': live()['guard'], 'visible_trees_checked': [34],
         'preserve_graphs': True, 'retain_hdwork_history': True}
    p['sha256'] = delete.digest(p)
    return p


def confirmation():
    return BeautifulSoup('<form action="host.php" method="post"><input name="__csrf_magic" value="secret">'
        '<ul><li>[HD-D] 1165651712_BANK JATIM_KF KENJERAN</li></ul>'
        '<input name="delete_type" type="radio" value="1"> Leave all <strong>Graph(s)</strong> and <strong>Data Source(s)</strong> untouched. Data Source(s) will be disabled however.'
        '<input name="delete_type" type="radio" value="2"> Delete all associated Graph(s) and Data Source(s).'
        '<input name="action" value="actions"><input name="selected_items" value=\'a:1:{i:0;s:4:"8471";}\'><input name="drp_action" value="1"></form>', 'html.parser')


class DeleteTests(unittest.TestCase):
    def test_exact_single_host_and_graph_preservation_confirmation(self):
        action, payload = delete.confirmation_payload(confirmation(), plan())
        self.assertEqual(action, 'host.php')
        self.assertEqual(payload['delete_type'], '1')
        self.assertEqual(payload['selected_items'], 'a:1:{i:0;s:4:"8471";}')
        self.assertNotIn('chk_3199', payload)
        for mutate in (
            lambda s: s.select_one('[name=selected_items]').__setitem__('value', 'a:1:{i:0;s:4:"3199";}'),
            lambda s: s.select_one('[name=selected_items]').__setitem__('value', 'a:2:{i:0;s:4:"8471";i:1;s:4:"3199";}'),
            lambda s: s.select_one('[name=drp_action]').__setitem__('value', '3'),
            lambda s: s.select_one('li').string.replace_with('ATCS'),
            lambda s: s.select_one('[name=delete_type][value="1"]').__setitem__('value', '3'),
        ):
            soup = confirmation(); mutate(soup)
            with self.assertRaises(RuntimeError): delete.confirmation_payload(soup, plan())
        soup = BeautifulSoup(str(confirmation()).replace('Leave all', 'Delete all'), 'html.parser')
        with self.assertRaises(RuntimeError): delete.confirmation_payload(soup, plan())

    def test_ok_conflict_wrong_mapping_and_different_source_block_delete(self):
        source = {'spreadsheet_id': 'sheet', 'snapshot_at': 'now', 'customers': [], 'conflicts': []}
        mappings = {'spreadsheet_id': 'sheet', 'mappings': [{**delete.TARGET, 'ownership_status': 'VERIFIED', 'verified_at': 'now'}]}
        api = Mock(); api.request.side_effect = [source, mappings]
        self.assertEqual(delete.source_check(api), source)
        for kind in ('OK', 'conflict', 'host', 'unverified', 'source'):
            s, m = copy.deepcopy(source), copy.deepcopy(mappings)
            if kind == 'OK': s['customers'] = [{'sid': '1165651712'}]
            if kind == 'conflict': s['conflicts'] = [{'sid': '1165651712'}]
            if kind == 'host': m['mappings'][0]['host_id'] = 3199
            if kind == 'unverified': m['mappings'][0]['ownership_status'] = 'REVIEW_REQUIRED'
            if kind == 'source': m['spreadsheet_id'] = 'different'
            api.request.side_effect = [s, m]
            with self.assertRaises(RuntimeError): delete.source_check(api)

    def test_absence_alone_is_not_not_evidence_and_changed_plan_is_rejected(self):
        p = plan(); delete.validate_plan(p)
        for change in ({'evidence': 'ABSENT'}, {'target': {**delete.TARGET, 'host_id': 3199}}, {'preserve_graphs': False}):
            with self.assertRaises(RuntimeError): delete.validate_plan({**p, **change})

    def test_other_tree_references_and_incomplete_tree_list_block(self):
        c = Mock(base='http://cacti/ont')
        c.session.get.return_value = Mock(text='<a href="tree.php?action=edit&id=34">DATIN SBS</a><a href="tree.php?action=edit&id=14">BGES SBS</a>')
        with patch.object(delete, 'parse_tree_memberships', return_value={8471: [{'tree_id': 14, 'header_id': 3707}]}):
            with self.assertRaises(RuntimeError): delete.tree_audit(c, {34, 14})
        with self.assertRaises(RuntimeError): delete.tree_audit(c, {34})
        c.session.post.assert_not_called()

    def test_verify_absence_requires_every_other_host_and_header_unchanged(self):
        for kind in ('ok', 'present', 'other-deleted', 'header-changed', 'orphan-reference'):
            c = Mock(base='http://cacti/ont')
            state = {'plan': plan(), 'before_host_ids': [1, 8471, 3199], 'before_headers': {'BANK JATIM': [{'header_id': 9127}]}}
            c.headers.return_value = {} if kind == 'header-changed' else state['before_headers']
            c.session.get.return_value = Mock(text='tree')
            ids = [1, 3199, 8471] if kind == 'present' else [1] if kind == 'other-deleted' else [1, 3199]
            refs = {8471: [{'header_id': 9127}]} if kind == 'orphan-reference' else {}
            with tempfile.TemporaryDirectory() as d, patch.object(delete, 'fetch_inventory', return_value=[{'host_id': i} for i in ids]), patch.object(delete, 'checked_html'), patch.object(delete, 'parse_tree_memberships', return_value=refs), contextlib.redirect_stdout(io.StringIO()):
                journal = Path(d) / 'delete.report.json'
                if kind == 'ok':
                    delete.verify_deleted(c, state, journal)
                    self.assertEqual(json.loads(journal.read_text())['phase'], 'VERIFIED')
                else:
                    with self.assertRaises(RuntimeError): delete.verify_deleted(c, state, journal)
            c.session.post.assert_not_called()

    def test_final_delete_intent_saved_before_single_post_and_no_api_database_writes(self):
        c, api = Mock(base='http://cacti/ont'), Mock()
        p, soup = plan(), confirmation()
        prepared = (Mock(url='http://cacti/ont/host.php'), soup, {'plan': p, 'phase': 'CONFIRMATION_PREPARED'})
        c.headers.return_value = {'BANK JATIM': [{'header_id': 9127}]}
        with tempfile.TemporaryDirectory() as d, patch.object(delete, 'prepare_confirmation', return_value=prepared), patch.object(delete, 'fetch_inventory', return_value=[{'host_id': 8471}, {'host_id': 3199}]), patch.object(delete, 'source_check'), patch.object(delete, 'read_live', return_value=live()), patch.object(delete, 'verify_deleted') as verify:
            journal = Path(d) / 'journal'
            def post(*args, **kwargs):
                self.assertEqual(json.loads(journal.read_text())['phase'], 'DELETE_CONFIRM_ATTEMPTED')
                self.assertEqual(kwargs['data']['delete_type'], '1')
                return Mock()
            c.session.post.side_effect = post
            delete.execute(c, api, p, journal)
            verify.assert_called_once()
        self.assertEqual(c.session.post.call_count, 1)
        api.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
