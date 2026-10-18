from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integrations/cacti-agent'))
from pilot_preview import creation_profile, select_pilot


def plan(**overrides):
    row = {'server': 'SBS', 'sid': '000123', 'institution': 'BANK JATIM', 'description': '[HD-D] 000123_BANK JATIM_CAPEM',
           'header_id': 9127, 'external_devices': [], 'actions': ['CREATE_DEVICE', 'ATTACH_TO_TREE'],
           'desired': {'olt_ip': '172.23.225.48', 'slot': '0', 'port': '0', 'onu': '0', 'serial_number': 'ZTEG0011'}}
    return {**row, **overrides}


def form():
    # Match legacy malformed markup: form closes before visible controls.
    html = '<form action="host.php"><input name="__csrf_magic" value="DO_NOT_EXPORT"></form>'
    for name in ('description', 'hostname', 'slot', 'port', 'onu_id', 'onu_sn'):
        limit = 3 if name in ('slot', 'port', 'onu_id') else 250
        html += f'<input name="{name}" value="" maxlength="{limit}">'
    for name, value in {'id': '0', 'action': 'save', 'save_component_host': '1', 'ping_timeout': '400', 'ping_retries': '1'}.items():
        html += f'<input name="{name}" value="{value}">'
    for name, value in {'host_template_id': '0', 'availability_method': '3', 'ping_method': '1', 'snmp_version': '0'}.items():
        html += f'<select name="{name}"><option value="{value}">default</option></select>'
    return html + '<input name="monitor" type="checkbox"><input name="disabled" type="checkbox"><textarea name="monitor_text"></textarea><input name="snmp_password" value="DO_NOT_EXPORT">'


class PilotTests(unittest.TestCase):
    def test_profile_reads_legacy_html_without_exporting_secrets(self):
        profile = creation_profile(form(), plan())
        self.assertEqual(profile['slot'], '0')
        self.assertTrue(profile['monitor'])
        self.assertFalse(profile['disabled'])
        self.assertEqual(profile['host_template_id'], '0')
        self.assertNotIn('DO_NOT_EXPORT', str(profile))

    def test_changed_form_defaults_and_existing_host_are_rejected(self):
        for html in (form().replace('name="id" value="0"', 'name="id" value="42"'),
                     form().replace('name="snmp_version"><option value="0"', 'name="snmp_version"><option value="2"'),
                     form().replace('name="description" value=""', 'name="description" value="existing"'),
                     form() + '<input name="id" value="0">'):
            with self.assertRaises(RuntimeError):
                creation_profile(html, plan())

    def test_overlong_description_or_location_never_silently_truncated(self):
        for row in (plan(description='x' * 251), plan(desired={**plan()['desired'], 'onu': '1000'})):
            with self.assertRaisesRegex(RuntimeError, 'batas'):
                creation_profile(form(), row)

    def test_selection_prefers_no_external_and_requires_existing_header(self):
        external = plan(sid='000001', external_devices=[{'host_id': 9}])
        self.assertEqual(select_pilot([external, plan()], 'SBS')['sid'], '000123')
        self.assertEqual(select_pilot([external, plan()], 'SBS', '000001')['sid'], '000001')
        for row in (plan(header_id=None), plan(actions=['HOLD']), plan(host_id=42), plan(server='SBU')):
            with self.assertRaises(RuntimeError):
                select_pilot([row], 'SBS')


if __name__ == '__main__':
    unittest.main()
