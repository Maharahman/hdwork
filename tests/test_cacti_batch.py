import copy
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
import tempfile
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integrations/cacti-agent'))
from batch_preview import header_dependencies, select_batch
import batch_preview as batch
from test_cacti_pilot import form


def rows():
    return [{'server': server, 'tree_id': tree, 'sid': f'{server}-{name}-{i}', 'institution': name,
             'header_id': None, 'actions': ['CREATE_HEADER', 'CREATE_DEVICE', 'ATTACH_TO_TREE'],
             'reasons': [], 'external_devices': []}
            for server, tree in (('SBS', 34), ('SBU', 28)) for name in ('BANK A', 'BANK B') for i in range(4)]


class BatchTests(unittest.TestCase):
    def test_balanced_ten_only_and_header_dependencies_are_unique(self):
        candidates = rows()
        before = copy.deepcopy(candidates)
        selected = select_batch(candidates)
        self.assertEqual(len(selected), 10)
        self.assertEqual(len({row['sid'] for row in selected}), 10)
        self.assertEqual(sum(row['server'] == 'SBS' for row in selected), 5)
        self.assertEqual(len(header_dependencies(selected)), 4)
        self.assertEqual(sorted(len(h['device_sids']) for h in header_dependencies(selected)), [2, 2, 3, 3])
        self.assertEqual(before, candidates)

    def test_existing_or_protected_or_wrong_tree_or_hold_never_selected(self):
        candidates = rows()
        bad = []
        for overrides in ({'header_id': 99}, {'host_id': 42}, {'institution': 'ATCS'}, {'tree_id': 999}, {'actions': ['HOLD']}, {'reasons': ['blocked']}):
            bad.append({**candidates[0], 'sid': 'bad-' + str(len(bad)), **overrides})
        self.assertTrue(all(not row['sid'].startswith('bad-') for row in select_batch(bad + candidates)))

    def test_insufficient_or_duplicate_sids_fail_instead_of_expanding_scope(self):
        with self.assertRaisesRegex(RuntimeError, '5 SID'):
            select_batch(rows()[:4])
        duplicates = [{**row, 'sid': 'same'} for row in rows()]
        with self.assertRaises(RuntimeError):
            select_batch(duplicates)

    def test_live_preview_only_reads_and_rejects_source_changed_during_scan(self):
        customers = [{"sid": row['sid'], 'server': row['server'], 'customer_name': row['institution'], 'source_row': i + 2,
                      'customer_detail': 'CAPEM', 'status': 'OK', 'olt_ip': '172.23.225.48', 'slot': '0', 'port': '0', 'onu': '0', 'serial_number': 'ZTEG0011'}
                     for i, row in enumerate(rows())]
        for changed in (False, True):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'batch.report.json'
                api = Mock()
                source_reads = []
                def request(method, path):
                    self.assertEqual(method, 'GET')
                    if path.endswith('device-source'):
                        source_reads.append(path)
                        return {'spreadsheet_id': 'sheet', 'snapshot_at': 'changed' if changed and len(source_reads) > 1 else 'now', 'customers': customers}
                    self.assertTrue(path.endswith('device-mappings'))
                    return {'spreadsheet_id': 'sheet', 'snapshot_at': 'now', 'mappings': []}
                api.request.side_effect = request
                clients = []
                def client(server):
                    c = Mock(server=server, base='http://cacti/ont')
                    c.headers.return_value = {}
                    c.session.get.return_value = Mock(text=form())
                    clients.append(c)
                    return c
                with patch.object(batch, 'HDWork', return_value=api), patch.object(batch, 'Cacti', side_effect=client), patch.object(batch, 'fetch_inventory', return_value=[]), patch.object(batch, 'load_dotenv'), patch.object(sys, 'argv', ['batch', '--report', str(output)]), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = batch.main()
                self.assertEqual(code, 1 if changed else 0)
                self.assertEqual(output.exists(), not changed)
                if not changed:
                    report = json.loads(output.read_text())
                    self.assertFalse(report['approved'])
                    self.assertFalse(report['executable'])
                    self.assertEqual(report['summary']['CREATE_HEADER_UNIQUE'], 4)
                    self.assertEqual(len(report['devices']), 10)
                for c in clients:
                    c.session.post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
