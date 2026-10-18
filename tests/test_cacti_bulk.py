import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integrations/cacti-agent'))
import bulk_sync as bulk
from test_cacti_pilot import form, plan
from pilot_preview import creation_profile


def row():
    r = plan(tree_id=34)
    r['creation_profile'] = creation_profile(form(), r)
    return r


class BulkTests(unittest.TestCase):
    def test_select_all_or_limit_excludes_hold_mapped_atcs(self):
        rows = [plan(tree_id=34, sid=str(i)) for i in range(30)]
        rows += [plan(tree_id=34, sid='held', actions=['HOLD']), plan(tree_id=34, sid='mapped', host_id=42), plan(tree_id=34, sid='atcs', institution='ATCS')]
        selected = bulk.select_rows({'rows': rows}, {'SBS': form()})
        self.assertEqual(len(selected), 30)
        self.assertEqual(len(bulk.select_rows({'rows': rows}, {'SBS': form()}, 20)), 20)
        self.assertNotIn('DO_NOT_EXPORT', json.dumps(selected))

    def test_private_atomic_journal_and_hash(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'job.report.json'
            bulk.durable(p, {'token': 'secret', 'phase': 'RESERVED'})
            bulk.durable(p, {'token': 'secret', 'phase': 'CREATE_ATTEMPTED'})
            self.assertEqual(json.loads(p.read_text())['phase'], 'CREATE_ATTEMPTED')
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(d).iterdir()), [p])
            r = row()
            p = {'version': 1, 'manifest': [r], 'manifest_sha256': bulk.digest([r])}
            bulk.validate_plan(p)
            p['manifest'][0]['desired']['slot'] = '9'
            with self.assertRaises(RuntimeError):
                bulk.validate_plan(p)

    def test_timeout_create_is_not_reposted_and_missing_result_stops(self):
        c = Mock(server='SBS', tree=34, base='http://cacti/ont')
        c.session.get.return_value = Mock(text=form(), url=c.base + '/host.php')
        c.session.post.side_effect = TimeoutError()
        state, save, api = {'phase': 'RESERVED'}, Mock(), Mock()
        with patch.object(bulk, 'fetch_inventory', return_value=[]), patch.object(bulk.time, 'sleep'):
            with self.assertRaises(TimeoutError):
                bulk.reconcile_device(c, row(), state, save, api, {}, 2)
            self.assertEqual(state['phase'], 'CREATE_ATTEMPTED')
            with self.assertRaisesRegex(RuntimeError, 'tidak diulang'):
                bulk.reconcile_device(c, row(), state, save, api, {}, 2)
        self.assertEqual(c.session.post.call_count, 1)
        api.request.assert_not_called()

    def test_resume_existing_created_attached_host_only_replays_mapping(self):
        for phase in ('CREATE_ATTEMPTED', 'ATTACH_ATTEMPTED', 'TREE_READ_BACK', 'VERIFIED'):
            c = Mock(server='SBS', tree=34, base='http://cacti/ont')
            c.session.get.return_value = Mock(text='tree')
            state, api = {'phase': phase}, Mock()
            with patch.object(bulk, 'fetch_inventory', return_value=[{'host_id': 42, 'description': row()['description']}]), patch.object(bulk, 'verify_host', return_value={'ok': True}), patch.object(bulk, 'checked_html'), patch.object(bulk, 'parse_tree_memberships', return_value={42: [{'header_id': 9127}]}):
                bulk.reconcile_device(c, row(), state, Mock(), api, {'job_id': 'j', 'token': 't'}, 2)
            self.assertEqual(state['phase'], 'VERIFIED')
            c.session.post.assert_not_called()
            self.assertEqual(api.request.call_args.args[1], '/api/datin-cacti/pilot-completions')

    def test_resume_unknown_duplicate_and_changed_hosts_never_write(self):
        for phase, matches in [('RESERVED', [{'host_id': 42}]), ('CREATE_ATTEMPTED', [{'host_id': 42}, {'host_id': 43}]), ('HOST_READ_BACK', [{'host_id': 43}])]:
            c, api = Mock(), Mock()
            state = {'phase': phase, 'host_id': 42}
            matches = [{**m, 'description': row()['description']} for m in matches]
            with patch.object(bulk, 'fetch_inventory', return_value=matches), self.assertRaises(RuntimeError):
                bulk.reconcile_device(c, row(), state, Mock(), api, {}, 2)
            c.session.post.assert_not_called()
            api.request.assert_not_called()

    def test_uncertain_missing_attach_is_not_retried(self):
        c, api = Mock(server='SBS', tree=34, base='http://cacti/ont'), Mock()
        c.session.get.return_value = Mock(text='tree')
        with patch.object(bulk, 'fetch_inventory', return_value=[{'host_id': 42, 'description': row()['description']}]), patch.object(bulk, 'verify_host'), patch.object(bulk, 'checked_html'), patch.object(bulk, 'parse_tree_memberships', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'Attach pernah'):
                bulk.reconcile_device(c, row(), {'phase': 'ATTACH_ATTEMPTED'}, Mock(), api, {}, 2)
        c.session.post.assert_not_called()

    def test_missing_backend_fails_before_cacti_login(self):
        import contextlib
        import io
        api = Mock()
        api.request.side_effect = RuntimeError('404')
        with patch.object(bulk, 'HDWork', return_value=api), patch.object(bulk, 'Cacti') as c, patch.object(bulk, 'single_instance', return_value=contextlib.nullcontext()), patch.object(bulk, 'load_dotenv'), patch.object(sys, 'argv', ['bulk', '--apply']), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bulk.main(), 1)
        c.assert_not_called()

    def test_uncertain_header_stops_before_any_create_and_token_is_reused(self):
        r = row()
        r['header_id'] = None
        p = {'version': 1, 'spreadsheet_id': 'sheet', 'manifest': [r], 'manifest_sha256': bulk.digest([r])}
        state = {'plan': p, 'batch_id': 'bulk-id', 'token': 'secret', 'snapshot_at': 'old', 'headers': {'SBS:BANK JATIM': {'phase': 'CREATE_ATTEMPTED', 'header_id': None}}, 'devices': {}}
        api = Mock()
        api.request.side_effect = [{'spreadsheet_id': 'sheet', 'snapshot_at': 'new'}, {'jobs': []}, {'spreadsheet_id': 'sheet'}]
        c = Mock()
        c.headers.return_value = {'BANK JATIM': [{'header_id': 99}]}
        with tempfile.TemporaryDirectory() as d, patch.object(bulk, 'validate_source'), self.assertRaisesRegex(RuntimeError, 'header belum pasti'):
            bulk.execute(api, {'SBS': c}, state, Path(d) / 'journal', 2)
        c.ensure_header.assert_not_called()
        self.assertEqual(api.request.call_args_list[1].args[2]['token'], 'secret')
        self.assertEqual(api.request.call_args_list[1].args[2]['snapshot_at'], 'new')

    def test_fresh_device_creates_then_attaches_with_durable_phase_before_each_post(self):
        c = Mock(server='SBS', tree=34, base='http://cacti/ont')
        c.session.get.return_value = Mock(text=form(), url=c.base + '/host.php')
        state, saved, api = {'phase': 'RESERVED'}, [], Mock()
        posted = []
        c.session.post.side_effect = lambda *a, **kw: (posted.append(state['phase']) or Mock())
        with patch.object(bulk, 'fetch_inventory', side_effect=[[], [{'host_id': 42, 'description': row()['description']}]]), patch.object(bulk, 'verify_host', return_value={'verified': True}), patch.object(bulk, 'checked_html'), patch.object(bulk, 'parse_tree_memberships', side_effect=[{}, {42: [{'header_id': 9127}]}]), patch.object(bulk, 'attach_payload', return_value=('tree.php', {})), patch.object(bulk.time, 'sleep') as sleep:
            bulk.reconcile_device(c, row(), state, lambda: saved.append(state['phase']), api, {'job_id': 'j', 'token': 't'}, 2)
        self.assertEqual(posted, ['CREATE_ATTEMPTED', 'ATTACH_ATTEMPTED'])
        self.assertEqual(state['phase'], 'VERIFIED')
        self.assertEqual(sleep.call_count, 2)
        self.assertIn('TREE_READ_BACK', saved)

    def test_execute_shared_new_header_once_and_then_resume_without_more_creates(self):
        rows = [row(), {**row(), 'sid': '000456', 'description': '[HD-D] 000456_BANK JATIM_CAPEM'}]
        for r in rows:
            r['header_id'] = None
            r['creation_profile'] = creation_profile(form(), r)
        p = {'version': 1, 'spreadsheet_id': 'sheet', 'manifest': rows, 'manifest_sha256': bulk.digest(rows)}
        state = {'plan': p, 'batch_id': 'bulk-id', 'token': 'secret', 'headers': {}, 'devices': {}}
        api, c = Mock(), Mock(server='SBS')
        source = {'spreadsheet_id': 'sheet', 'snapshot_at': 'now', 'aliases': []}
        jobs = [{'sid': r['sid'], 'server': 'SBS'} for r in rows]
        api.request.side_effect = lambda method, path, payload=None: source if method == 'GET' else {'jobs': jobs} if path.endswith('bulk-reservations') else {'success': True}
        live = {}
        c.headers.side_effect = lambda: copy.deepcopy(live)
        def create(name, apply=False):
            live[name] = [{'header_id': 99}]
            return {'header_id': 99, 'action': 'created'}
        c.ensure_header.side_effect = create
        def reconcile(client, r, progress, save, *args):
            self.assertEqual(r['header_id'], 99)
            progress['phase'] = 'VERIFIED'
            save()
        with tempfile.TemporaryDirectory() as d, patch.object(bulk, 'validate_source'), patch.object(bulk, 'reconcile_device', side_effect=reconcile), patch.object(bulk.time, 'sleep'), patch('builtins.print'):
            journal = Path(d) / 'job'
            bulk.execute(api, {'SBS': c}, state, journal, 2)
            bulk.execute(api, {'SBS': c}, json.loads(journal.read_text()), journal, 2)
        self.assertEqual(c.ensure_header.call_count, 1)
        self.assertEqual(api.request.call_args.args[1], '/api/datin-cacti/batch-completions')


if __name__ == '__main__':
    unittest.main()
