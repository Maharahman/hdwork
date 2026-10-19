import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integrations/cacti-agent'))
import edit_device as edit
from compare_devices import parse_device_detail


def form(slot='8', port='6', onu='19'):
    html = '<form method="post" action="host.php"><input name="__csrf_magic" value="SECRET-CSRF"></form>'
    values = {'id': '8471', 'save_component_host': '1', 'action': 'save',
              'description': '[HD-D] 1165651712_BANK JATIM_KF KENJERAN', 'hostname': '172.23.225.42',
              'slot': slot, 'port': port, 'onu_id': onu, 'onu_sn': 'ZTEGCC82D5F7',
              'snmp_community': 'SECRET-SNMP', 'snmp_password': 'SECRET-PASSWORD', 'ping_timeout': '400'}
    for name, value in values.items():
        html += f'<input name="{name}" value="{value}" maxlength="{3 if name in ("slot", "port", "onu_id") else 250}">'
    return html + '<input name="monitor" type="checkbox" checked><input name="disabled" type="checkbox"><select name="host_template_id"><option value="0" selected>None</option></select><textarea name="monitor_text">metrics</textarea><textarea name="notes">ONLINE</textarea>'


def live(html=None):
    html = html or form()
    soup, action, payload = edit.parse_form(html)
    return {'soup': soup, 'action': action, 'payload': payload, 'detail': parse_device_detail(html, 8471),
            'guard': edit.guard(payload), 'response': Mock(url='http://cacti/ont/host.php')}


def plan():
    return edit.make_plan({'spreadsheet_id': 'sheet', 'snapshot_at': 'now'}, live(form('9', '7'))['detail'], live())


class EditTests(unittest.TestCase):
    def test_plan_only_slot_port_onu_and_never_exports_secrets(self):
        p = plan()
        self.assertEqual(p['changes'], {'slot': {'before': '8', 'after': '9'}, 'port': {'before': '6', 'after': '7'}})
        self.assertNotIn('SECRET', json.dumps(p))
        for key in ('description', 'hostname', 'onu_sn'):
            after = live(form('9', '7'))['detail']
            after[key] = 'changed'
            with self.assertRaisesRegex(RuntimeError, 'hanya'):
                edit.make_plan({'spreadsheet_id': 'sheet', 'snapshot_at': 'now'}, after, live())
        p['after']['slot'] = '0'
        with self.assertRaises(RuntimeError):
            edit.validate_plan(p)

    def test_payload_preserves_all_options_except_three_fields(self):
        c = Mock()
        before, after = live(), live(form('9', '7'))
        with tempfile.TemporaryDirectory() as d, patch.object(edit, 'read_live', side_effect=[before, after]), contextlib.redirect_stdout(io.StringIO()):
            journal = Path(d) / 'edit.report.json'
            c.session.post.side_effect = lambda *a, **kw: (self.assertEqual(json.loads(journal.read_text())['phase'], 'EDIT_ATTEMPTED') or Mock())
            edit.execute(c, plan(), journal)
            payload = c.session.post.call_args.kwargs['data']
            self.assertEqual({k for k in payload if payload[k] != before['payload'][k]}, {'slot', 'port'})
            self.assertEqual(payload['monitor'], 'on')
            self.assertNotIn('disabled', payload)
            self.assertEqual(payload['snmp_password'], 'SECRET-PASSWORD')
            self.assertEqual(json.loads(journal.read_text())['phase'], 'VERIFIED')
            self.assertNotIn('SECRET', journal.read_text())
        c.session.post.assert_called_once()

    def test_timeout_preserves_intent_and_resume_never_reposts(self):
        c = Mock()
        c.session.post.side_effect = TimeoutError()
        with tempfile.TemporaryDirectory() as d, patch.object(edit, 'read_live', return_value=live()):
            journal = Path(d) / 'edit.report.json'
            with self.assertRaises(TimeoutError):
                edit.execute(c, plan(), journal)
            state = json.loads(journal.read_text())
            self.assertEqual(state['phase'], 'EDIT_ATTEMPTED')
            with self.assertRaisesRegex(RuntimeError, 'tidak diulang'):
                edit.execute(c, plan(), journal, state)
            with patch.object(edit, 'read_live', return_value=live(form('9', '7'))), contextlib.redirect_stdout(io.StringIO()):
                edit.execute(c, plan(), journal, state)
            self.assertEqual(json.loads(journal.read_text())['phase'], 'VERIFIED')
        self.assertEqual(c.session.post.call_count, 1)

    def test_concurrent_host_option_or_source_changes_stop_before_write(self):
        for changed in (form('7', '6'), form().replace('value="400"', 'value="500"')):
            c = Mock()
            with tempfile.TemporaryDirectory() as d, patch.object(edit, 'read_live', return_value=live(changed)), self.assertRaises(RuntimeError):
                edit.execute(c, plan(), Path(d) / 'journal')
            c.session.post.assert_not_called()
        p = plan()
        for desired in (live()['detail'], {**p['after'], 'onu_id': '20'}):
            with self.assertRaises(RuntimeError):
                edit.verify_desired(p, {'spreadsheet_id': 'sheet'}, desired)

    def test_polling_runtime_and_csrf_changes_do_not_change_option_guard(self):
        original = live()
        changed = live(form().replace('SECRET-CSRF', 'OTHER-CSRF').replace('>ONLINE<', '>LOS<').replace('>metrics<', '>newmetrics<'))
        self.assertEqual(original['guard'], changed['guard'])

    def test_wrong_mapping_status_or_conflict_is_rejected_and_zero_is_valid(self):
        customer = {'sid': '1165651712', 'server': 'SBS', 'customer_name': 'BANK JATIM', 'customer_detail': 'KF KENJERAN',
                    'status': 'OK', 'olt_ip': '172.23.225.42', 'slot': '0', 'port': '0', 'onu': '0', 'serial_number': 'ZTEGCC82D5F7'}
        source = {'spreadsheet_id': 'sheet', 'customers': [customer], 'conflicts': []}
        mapping = {'spreadsheet_id': 'sheet', 'mappings': [{**edit.TARGET, 'ownership_status': 'VERIFIED', 'verified_at': 'now'}]}
        api = Mock()
        api.request.side_effect = [source, mapping]
        self.assertEqual(edit.load_source(api)[1]['slot'], '0')
        for mutation in ('host', 'status', 'conflict', 'server'):
            s, m = copy.deepcopy(source), copy.deepcopy(mapping)
            if mutation == 'host': m['mappings'][0]['host_id'] = 3199
            if mutation == 'server': m['mappings'][0]['server'] = 'SBU'
            if mutation == 'status': s['customers'][0]['status'] = 'NOT'
            if mutation == 'conflict': s['conflicts'] = [{'sid': '1165651712'}]
            api.request.side_effect = [s, m]
            with self.assertRaises(RuntimeError):
                edit.load_source(api)

    def test_duplicate_controls_wrong_host_or_wrong_action_fail_closed(self):
        for html in (form() + '<input name="id" value="8471">', form().replace('value="8471"', 'value="3199"'), form().replace('value="save"', 'value="delete"'), form() + '<input name="slot" value="8">'):
            with self.assertRaises(RuntimeError):
                edit.parse_form(html)


if __name__ == '__main__':
    unittest.main()
