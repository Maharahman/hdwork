import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integrations/cacti-agent'))
import batch_apply as batch
from pilot_preview import creation_profile
from test_cacti_pilot import form


def fixtures():
    customers, manifest = [], []
    for target in batch.CONFIG['targets']:
        desired = {'olt_ip': '172.23.225.48', 'slot': '0', 'port': '0', 'onu': '0', 'serial_number': 'TESTSN'}
        row = {**target, 'description': f"[HD-D] {target['sid']}_{target['institution']}_TEST", 'desired': desired}
        row['creation_profile'] = creation_profile(form(), row)
        manifest.append(row)
        customers.append({'sid': row['sid'], 'server': row['server'], 'customer_name': row['institution'], 'customer_detail': 'TEST', 'status': 'OK', **desired})
    settings = {**batch.CONFIG, 'approved_sha256': hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()}
    return manifest, customers, settings


class BatchApplyTests(unittest.TestCase):
    def test_manifest_and_source_are_bound_to_exact_approved_config(self):
        manifest, customers, settings = fixtures()
        with patch.object(batch, 'CONFIG', settings):
            self.assertEqual(len(batch.freeze_manifest(manifest)), 10)
            batch.validate_source(manifest, {'customers': customers})
            for changed in (manifest[:9], [manifest[0]] * 10):
                with self.assertRaises(RuntimeError):
                    batch.freeze_manifest(changed)
            changed = copy.deepcopy(manifest)
            changed[0]['desired']['slot'] = '9'
            with self.assertRaisesRegex(RuntimeError, 'Konfigurasi'):
                batch.freeze_manifest(changed)
            source = copy.deepcopy(customers)
            source[0]['status'] = 'NOT'
            with self.assertRaises(RuntimeError):
                batch.validate_source(manifest, {'customers': source})

    def test_missing_backend_stops_before_any_cacti_client(self):
        manifest, _, settings = fixtures()
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / 'preview.json'
            preview.write_text(json.dumps({'devices': manifest}))
            api = Mock()
            api.request.side_effect = RuntimeError('HTTP 404')
            with patch.object(batch, 'CONFIG', settings), patch.object(batch, 'HDWork', return_value=api), patch.object(batch, 'Cacti') as client, patch.object(batch, 'load_dotenv'), patch.object(sys, 'argv', ['batch', '--preview', str(preview), '--apply']), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(batch.main(), 1)
            client.assert_not_called()
            api.request.assert_called_once_with('GET', '/api/datin-cacti/batch-capabilities')

    def test_preflight_full_batch_and_uncertain_header_creation_are_bounded(self):
        for mode in ('preflight', 'apply', 'header-timeout', 'reserve-failed'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                directory = Path(directory)
                manifest, customers, settings = fixtures()
                preview = directory / 'preview.json'
                preview.write_text(json.dumps({'devices': manifest, 'spreadsheet_id': 'sheet'}))
                writes, clients = [], {}
                api = Mock()
                def request(method, path, payload=None):
                    if method == 'GET':
                        if path.endswith('batch-capabilities'):
                            return {**settings, 'version': 1}
                        if path.endswith('device-source'):
                            return {'spreadsheet_id': 'sheet', 'snapshot_at': 'now', 'customers': customers}
                        return {'spreadsheet_id': 'sheet', 'mappings': []}
                    writes.append((path, payload))
                    if path.endswith('batch-reservations'):
                        if mode == 'reserve-failed':
                            raise RuntimeError('migration missing')
                        return {'token': 'secret', 'jobs': [{'sid': row['sid'], 'server': row['server'], 'job_id': str(i), 'token': 'job-secret'} for i, row in enumerate(manifest)]}
                    return {'success': True}
                api.request.side_effect = request
                def client(server):
                    c = Mock(server=server, tree=34 if server == 'SBS' else 28, base='http://cacti/ont')
                    live_headers = {}
                    c.headers.side_effect = lambda: copy.deepcopy(live_headers)
                    c.session.get.return_value = Mock(text=form())
                    def ensure_header(name, apply=False):
                        self.assertTrue(apply)
                        if mode == 'header-timeout':
                            raise TimeoutError('uncertain')
                        header = {'name': name, 'server': server, 'tree_id': c.tree, 'header_id': 100 + len(live_headers), 'action': 'created'}
                        live_headers[name] = [header]
                        return header
                    c.ensure_header.side_effect = ensure_header
                    clients[server] = c
                    return c
                apply_devices = Mock(side_effect=lambda *args: 1000 + apply_devices.call_count)
                argv = ['batch', '--preview', str(preview)] + ([] if mode == 'preflight' else ['--apply'])
                with patch.object(batch, 'CONFIG', settings), patch.object(batch, '__file__', str(directory / 'batch_apply.py')), patch.object(batch, 'HDWork', return_value=api), patch.object(batch, 'Cacti', side_effect=client), patch.object(batch, 'fetch_inventory', return_value=[]), patch.object(batch, 'execute_reserved_device', apply_devices), patch.object(batch, 'load_dotenv'), patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = batch.main()
                self.assertEqual(code, 0 if mode in ('preflight', 'apply') else 1)
                expected = 10 if mode == 'apply' else 0
                self.assertEqual(apply_devices.call_count, expected)
                self.assertEqual(sum(c.ensure_header.call_count for c in clients.values()), 4 if mode == 'apply' else 1 if mode == 'header-timeout' else 0)
                if mode == 'apply':
                    self.assertEqual(len([p for p, _ in writes if p.endswith('batch-headers')]), 4)
                    self.assertEqual({call.args[1]['sid'] for call in apply_devices.call_args_list}, {row['sid'] for row in manifest})
                    self.assertEqual(writes[-1][0], '/api/datin-cacti/batch-completions')
                    self.assertEqual(json.loads((directory / 'batch10-execution.report.json').read_text())['phase'], 'VERIFIED')
                if mode == 'preflight':
                    self.assertEqual(writes, [])


if __name__ == '__main__':
    unittest.main()
