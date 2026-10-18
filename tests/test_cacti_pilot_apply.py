import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integrations/cacti-agent'))
import pilot_apply as apply
from test_cacti_pilot import form, plan
from pilot_preview import creation_profile


class PilotApplyTests(unittest.TestCase):
    def test_new_payload_preserves_csrf_and_sets_only_approved_profile(self):
        row = plan()
        profile = creation_profile(form(), row)
        action, payload = apply.create_payload(form(), row, profile)
        self.assertEqual(action, 'host.php')
        self.assertEqual(payload['__csrf_magic'], 'DO_NOT_EXPORT')
        self.assertEqual(payload['monitor'], 'on')
        self.assertNotIn('disabled', payload)
        self.assertEqual(payload['slot'], '0')
        with self.assertRaisesRegex(RuntimeError, 'Profil'):
            apply.create_payload(form(), row, {**profile, 'monitor': False})

    def test_attach_requires_exact_tree_header_and_known_host(self):
        html = '<form action="tree.php"><input name="graph_tree_id" value="34"><input name="type" value="3"><input name="id" value="0"><input name="action" value="save"><input name="__csrf_magic" value="token"><select name="parent_item_id"><option value="9127" selected>BANK JATIM</option></select><select name="host_id"><option value="42">new</option></select></form>'
        soup = BeautifulSoup(html, 'html.parser')
        action, payload = apply.attach_payload(soup, {**plan(), 'tree_id': 34}, 42)
        self.assertEqual(payload['host_id'], '42')
        self.assertEqual(payload['parent_item_id'], '9127')
        self.assertEqual(payload['__csrf_magic'], 'token')
        for bad in (html.replace('value="34"', 'value="28"'), html.replace('value="9127"', 'value="9218"'), html.replace('value="42"', 'value="43"')):
            with self.assertRaises(RuntimeError):
                apply.attach_payload(BeautifulSoup(bad, 'html.parser'), {**plan(), 'tree_id': 34}, 42)

    def test_journal_exclusive_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'job.report.json'
            apply.save_journal(path, {'phase': 'RESERVED'}, exclusive=True)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                apply.save_journal(path, {}, exclusive=True)

    def test_missing_backend_stops_before_any_cacti_client(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'preview.json'
            path.write_text(json.dumps({'pilots': [{'server': 'SBS'}, {'server': 'SBU'}]}))
            api = Mock()
            api.request.side_effect = RuntimeError('HTTP 404')
            with patch.object(apply, 'HDWork', return_value=api), patch.object(apply, 'Cacti') as cacti, patch.object(apply, 'load_dotenv'), patch.object(sys, 'argv', ['pilot', '--preview', str(path), '--apply']), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(apply.main(), 1)
            cacti.assert_not_called()
            api.request.assert_called_once_with('GET', '/api/datin-cacti/pilot-capabilities')

    def test_preflight_and_failed_create_never_repeat_post_or_save_mapping(self):
        for execute in (False, True):
            with self.subTest(execute=execute), tempfile.TemporaryDirectory() as directory:
                directory = Path(directory)
                rows, customers = [], []
                for server, target in apply.TARGETS.items():
                    row = plan(server=server, sid=target['sid'], tree_id=34 if server == 'SBS' else 28, header_id=target['header_id'], description=f'[HD-D] {target["sid"]}_BANK JATIM_CAPEM')
                    row['creation_profile'] = creation_profile(form(), row)
                    rows.append(row)
                    customers.append({'sid': row['sid'], 'server': server, 'customer_name': 'BANK JATIM', 'customer_detail': 'CAPEM', 'status': 'OK', **row['desired']})
                preview = directory / 'pilot.report.json'
                preview.write_text(json.dumps({'spreadsheet_id': 'sheet', 'pilots': rows}))
                api = Mock()
                writes = []
                def request(method, path, payload=None):
                    if method == 'POST':
                        writes.append(path)
                        self.assertEqual(path, '/api/datin-cacti/pilot-reservations')
                        return {'job_id': 'job', 'token': 'secret'}
                    if path.endswith('pilot-capabilities'):
                        return {'version': 1, 'bulk_enabled': False}
                    if path.endswith('device-source'):
                        return {'spreadsheet_id': 'sheet', 'snapshot_at': 'now', 'customers': customers}
                    return {'spreadsheet_id': 'sheet', 'mappings': []}
                api.request.side_effect = request
                clients = []
                def client(server):
                    c = Mock(server=server, base='http://cacti/ont', tree=34 if server == 'SBS' else 28)
                    c.headers.return_value = {'BANK JATIM': [{'header_id': apply.TARGETS[server]['header_id']}]}
                    c.session.get.return_value = Mock(text=form(), url=c.base + '/host.php')
                    # An uncertain POST must stop, leaving reservation/journal intact.
                    c.session.post.side_effect = TimeoutError('uncertain create')
                    clients.append(c)
                    return c
                argv = ['pilot', '--preview', str(preview)] + (['--apply'] if execute else [])
                with patch.object(apply, '__file__', str(directory / 'pilot_apply.py')), patch.object(apply, 'HDWork', return_value=api), patch.object(apply, 'Cacti', side_effect=client), patch.object(apply, 'fetch_inventory', return_value=[]), patch.object(apply, 'load_dotenv'), patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = apply.main()
                self.assertEqual(code, 1 if execute else 0)
                self.assertEqual(sum(c.session.post.call_count for c in clients), 1 if execute else 0)
                self.assertEqual(writes, ['/api/datin-cacti/pilot-reservations'] if execute else [])
                if execute:
                    state = json.loads((directory / 'pilot-SBS.report.json').read_text())
                    self.assertEqual(state['phase'], 'CREATE_ATTEMPTED')

    def test_reserved_pipeline_creates_attaches_and_completes_once(self):
        for attach_fails in (False, True):
            with self.subTest(attach_fails=attach_fails), tempfile.TemporaryDirectory() as directory:
                row = {**plan(), 'tree_id': 34}
                row['creation_profile'] = creation_profile(form(), row)
                tree_form = '<form action="tree.php"><input name="graph_tree_id" value="34"><input name="type" value="3"><input name="id" value="0"><input name="action" value="save"><select name="parent_item_id"><option value="9127" selected>BANK JATIM</option></select><select name="host_id"><option value="42">new</option></select></form>'
                c = Mock(server='SBS', tree=34, base='http://cacti/ont')
                c.session.get.side_effect = [Mock(text=form(), url=c.base + '/host.php'), Mock(text=tree_form, url=c.base + '/tree.php'), Mock(text='<table></table>')]
                if attach_fails:
                    c.session.post.side_effect = [Mock(), TimeoutError('uncertain attach')]
                api = Mock()
                journal = Path(directory) / 'job.report.json'
                with patch.object(apply, 'fetch_inventory', return_value=[{'host_id': 42, 'description': row['description']}]), patch.object(apply, 'verify_host', return_value={'verified': True}) as verify, patch.object(apply, 'parse_tree_memberships', return_value={42: [{'header_id': 9127}]}):
                    if attach_fails:
                        with self.assertRaises(TimeoutError):
                            apply.execute_reserved_device(c, row, journal, {'job_id': 'job', 'token': 'secret'}, api)
                        api.request.assert_not_called()
                    else:
                        self.assertEqual(apply.execute_reserved_device(c, row, journal, {'job_id': 'job', 'token': 'secret'}, api), 42)
                        self.assertEqual(verify.call_count, 2)
                        api.request.assert_called_once_with('POST', '/api/datin-cacti/pilot-completions', {'job_id': 'job', 'token': 'secret', 'host_id': 42, 'receipt': {'verified': True}})
                self.assertEqual(c.session.post.call_count, 2)
                self.assertEqual(json.loads(journal.read_text())['phase'], 'ATTACH_ATTEMPTED' if attach_fails else 'VERIFIED')


if __name__ == '__main__':
    unittest.main()
