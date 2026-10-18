"""Prepare one device per server for review. No Cacti or database writes."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from bs4 import BeautifulSoup
from dotenv import load_dotenv

from compare_devices import FIELDS, compare, fetch_inventory
from device_plan import build_device_plan
from sync_headers import Cacti, HDWork, SERVERS, checked_html, normalize


def selected_value(soup, name):
    fields = soup.select(f'select[name="{name}"]')
    if len(fields) != 1:
        raise RuntimeError(f"Opsi {name} tidak ditemukan atau ganda.")
    option = fields[0].select_one('option[selected]') or fields[0].select_one('option')
    if option is None:
        raise RuntimeError(f"Opsi {name} kosong.")
    return option.get('value', '')


def creation_profile(html, plan):
    """Read document-wide controls: legacy Cacti has malformed form boundaries.

    Return only a review profile, never a submit payload or session/CSRF secrets.
    """
    soup = BeautifulSoup(html, 'html.parser')
    values = {'description': plan['description'], **{target: str(plan['desired'][source]) for source, target in FIELDS.items()}}
    for name, value in values.items():
        controls = soup.select(f'input[name="{name}"]')
        if len(controls) != 1 or controls[0].get('value', '').strip():
            raise RuntimeError(f"Form bukan device baru: {name} tidak kosong atau tidak dikenali.")
        limit = controls[0].get('maxlength')
        if limit and (not limit.isdigit() or len(value) > int(limit)):
            raise RuntimeError(f"Nilai {name} melebihi batas form; tidak dipotong otomatis.")
    for name, expected in {'id': '0', 'action': 'save', 'save_component_host': '1'}.items():
        control = soup.select(f'input[name="{name}"]')
        if len(control) != 1 or control[0].get('value') != expected:
            raise RuntimeError('Identitas form create tidak sesuai.')
    defaults = {name: selected_value(soup, name) for name in ('host_template_id', 'availability_method', 'ping_method', 'snmp_version')}
    if defaults != {'host_template_id': '0', 'availability_method': '3', 'ping_method': '1', 'snmp_version': '0'}:
        raise RuntimeError('Default monitoring berubah; perlu review manual, bukan overwrite otomatis.')
    for name in ('monitor', 'disabled'):
        field = soup.select(f'input[name="{name}"]')
        if len(field) != 1 or field[0].get('type') != 'checkbox':
            raise RuntimeError(f'Checkbox {name} tidak dikenali.')
    if len(soup.select('textarea[name="monitor_text"]')) != 1:
        raise RuntimeError('Down Host Message tidak dikenali.')
    timing = {}
    for name in ('ping_timeout', 'ping_retries'):
        field = soup.select(f'input[name="{name}"]')
        if len(field) != 1 or not field[0].get('value', '').isdigit():
            raise RuntimeError(f'Opsi {name} tidak valid.')
        timing[name] = field[0]['value']
    return {**values, **defaults, **timing, 'monitor': True, 'disabled': False,
            'monitor_text': plan['description'], 'host_template_label': 'None',
            'availability_label': 'Ping', 'ping_label': 'ICMP Ping', 'snmp_label': 'Not In Use'}


def select_pilot(rows, server, sid=None):
    possible = [row for row in rows if row['server'] == server and (sid is None or row['sid'] == sid)]
    # Avoid new headers, moves, edits and any ownership adoption in the first test.
    possible = [row for row in possible if row['actions'] == ['CREATE_DEVICE', 'ATTACH_TO_TREE'] and row.get('header_id') and not row.get('host_id')]
    if not possible:
        raise RuntimeError(f'Tidak ada calon create aman pada {server} untuk SID yang dipilih/header yang sudah ada.')
    return sorted(possible, key=lambda row: (bool(row['external_devices']), row['institution'] != 'BANK JATIM', row['sid']))[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', default=str(Path(__file__).with_name('.env')))
    parser.add_argument('--sbs-sid')
    parser.add_argument('--sbu-sid')
    parser.add_argument('--report', default=str(Path(__file__).with_name('pilot.report.json')))
    args = parser.parse_args()
    load_dotenv(args.env_file)
    clients = []
    api = None
    try:
        api = HDWork()
        source = api.request('GET', '/api/datin-cacti/device-source')
        mappings = api.request('GET', '/api/datin-cacti/device-mappings')
        if (source['snapshot_at'], source['spreadsheet_id']) != (mappings['snapshot_at'], mappings['spreadsheet_id']):
            raise RuntimeError('Snapshot sumber/mapping berbeda; ulangi.')
        inventory, headers = [], {}
        for server in SERVERS:
            client = Cacti(server)
            clients.append(client)
            client.login()
            inventory.extend(fetch_inventory(client))
            headers[server] = client.headers()
        aliases = {item['alias']: item['name'] for item in source.get('aliases', [])}
        comparison = compare(source['customers'], inventory, aliases, source_conflicts=source.get('conflicts'))
        plan = build_device_plan(comparison, mappings['mappings'], headers, inventory, SERVERS, aliases)
        pilots = []
        for client in clients:
            row = select_pilot(plan['rows'], client.server, getattr(args, client.server.lower() + '_sid'))
            response = client.session.get(client.base + '/host.php', params={'action': 'edit', 'host_template_id': -1, 'host_status': -1}, timeout=30)
            checked_html(response)
            pilots.append({**row, 'creation_profile': creation_profile(response.text, row)})
        current = api.request('GET', '/api/datin-cacti/device-source')
        current_mappings = api.request('GET', '/api/datin-cacti/device-mappings')
        if current['snapshot_at'] != source['snapshot_at'] or current_mappings != mappings:
            raise RuntimeError('Snapshot/mapping berubah selama scan; pratinjau tidak disimpan. Ulangi.')
        report = {'mode': 'PILOT_PREVIEW_ONLY', 'executable': False, 'approved': False,
                  'time': datetime.now(timezone.utc).isoformat(), 'spreadsheet_id': source['spreadsheet_id'],
                  'snapshot_at': source['snapshot_at'], 'pilots': pilots}
        Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
        for row in pilots:
            print(f"{row['server']}: SID {row['sid']} | header {row['header_id']} | {row['description']}")
        print(f'Pratinjau saja: {args.report}. Tidak ada device/header/mapping yang ditulis.')
        return 0
    except Exception as error:
        print(str(error) if isinstance(error, RuntimeError) else type(error).__name__, file=sys.stderr)
        return 1
    finally:
        for client in clients:
            client.close()
        if api:
            api.session.close()


if __name__ == '__main__':
    sys.exit(main())
