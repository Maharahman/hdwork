"""One guarded edit pilot: SID 1165651712, existing SBS host 8471 only."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from bulk_sync import durable, digest
from compare_devices import FIELDS, parse_device_detail, parse_tree_memberships, planned_description, validate_customer
from sync_headers import Cacti, HDWork, checked_html, form_payload, normalize, single_instance

TARGET = {'sid': '1165651712', 'server': 'SBS', 'host_id': 8471, 'tree_id': 34, 'header_id': 9127}
EDIT_FIELDS = {'slot': 'slot', 'port': 'port', 'onu': 'onu_id'}
BASE = Path(__file__).resolve().parent
DEFAULT_PLAN = BASE / 'edit-1165651712-plan.report.json'
DEFAULT_JOURNAL = BASE / 'edit-1165651712-execution.report.json'


def load_source(api):
    source = api.request('GET', '/api/datin-cacti/device-source')
    mapping = api.request('GET', '/api/datin-cacti/device-mappings')
    if source['spreadsheet_id'] != mapping['spreadsheet_id']:
        raise RuntimeError('Spreadsheet mapping/sumber berbeda.')
    matches = [r for r in source['customers'] if r['sid'] == TARGET['sid']]
    mapped = [r for r in mapping['mappings'] if r['sid'] == TARGET['sid']]
    if (len(matches) != 1 or len(mapped) != 1 or any(r['sid'] == TARGET['sid'] for r in source.get('conflicts', []))
            or any(mapped[0].get(k) != v for k, v in TARGET.items())
            or mapped[0].get('ownership_status') != 'VERIFIED' or not mapped[0].get('verified_at')):
        raise RuntimeError('SID/mapping pilot tidak unik atau bukan host VERIFIED yang disetujui.')
    row = matches[0]
    if (validate_customer(row) or normalize(row.get('status', '')) != 'OK'
            or normalize(row.get('server', '')) != TARGET['server'] or normalize(row.get('customer_name', '')) != 'BANK JATIM'):
        raise RuntimeError('Data sumber pilot berubah/tidak valid.')
    desired = {'description': planned_description(row), **{target: str(row[source]).strip() for source, target in FIELDS.items()}}
    if any(not value.isdigit() or len(value) > 3 for key, value in desired.items() if key in EDIT_FIELDS.values()):
        raise RuntimeError('Slot/port/ONU wajib 0–999; nol tetap valid.')
    return source, desired


def parse_form(html):
    soup = BeautifulSoup(html, 'html.parser')
    forms = soup.select('form[action]')
    if len(forms) != 1:
        raise RuntimeError('Form edit tidak dikenali/ganda.')
    for name, value in {'id': str(TARGET['host_id']), 'action': 'save', 'save_component_host': '1'}.items():
        fields = soup.select(f'input[name="{name}"]')
        if len(fields) != 1 or fields[0].get('value') != value:
            raise RuntimeError('Identitas form bukan edit host pilot.')
    # Legacy controls are outside the form element; preserve all successful controls.
    payload = form_payload(soup)
    for field in soup.select('textarea[name]'):
        payload[field['name']] = field.get_text()
    for field in soup.select('input[name]'):
        if field.get('type', '').lower() in ('checkbox', 'radio') and field.has_attr('checked'):
            payload[field['name']] = field.get('value', 'on')
    for name in ('description', *FIELDS.values()):
        if len(soup.select(f'input[name="{name}"]')) != 1:
            raise RuntimeError('Field konfigurasi edit hilang/ganda.')
    return soup, forms[0]['action'], payload


def guard(payload):
    # Runtime notes/metrics can change during polling. Never export SNMP secrets.
    excluded = {'__csrf_magic', 'slot', 'port', 'onu_id', 'notes', 'monitor_text'}
    return digest({k: v for k, v in payload.items() if k not in excluded})


def read_live(client):
    response = client.session.get(client.base + '/host.php', params={'action': 'edit', 'id': TARGET['host_id']}, timeout=30)
    checked_html(response)
    soup, action, payload = parse_form(response.text)
    detail = parse_device_detail(response.text, TARGET['host_id'])
    tree = client.session.get(client.base + '/tree.php', params={'action': 'edit', 'id': TARGET['tree_id']}, timeout=30)
    checked_html(tree)
    memberships = parse_tree_memberships(tree.text, TARGET['tree_id']).get(TARGET['host_id'], [])
    if len(memberships) != 1 or memberships[0]['header_id'] != TARGET['header_id'] or normalize(memberships[0]['header']) != 'BANK JATIM':
        raise RuntimeError('Header/tree pilot berubah; tidak memindahkan host.')
    # Also validate the configured tree name and that the header itself is unique.
    headers = client.headers().get('BANK JATIM', [])
    if len(headers) != 1 or headers[0]['header_id'] != TARGET['header_id']:
        raise RuntimeError('Header pilot ganda/berubah.')
    for name in ('monitor', 'disabled'):
        fields = soup.select(f'input[name="{name}"]')
        if len(fields) != 1 or fields[0].get('type', '').lower() != 'checkbox':
            raise RuntimeError('Opsi monitoring tidak dikenali.')
    if 'monitor' not in payload or 'disabled' in payload:
        raise RuntimeError('Host tidak dalam keadaan monitoring aktif.')
    return {'detail': detail, 'guard': guard(payload), 'response': response, 'action': action, 'payload': payload, 'soup': soup}


def make_plan(source, desired, live):
    before = live['detail']
    allowed = set(EDIT_FIELDS.values())
    if any(before[k] != desired[k] for k in desired if k not in allowed):
        raise RuntimeError('IP/SN/nama berubah; uji ini hanya mengizinkan slot/port/ONU.')
    changes = {k: {'before': before[k], 'after': desired[k]} for k in allowed if before[k] != desired[k]}
    if not changes:
        raise RuntimeError('Tidak ada perubahan slot/port/ONU; tidak perlu POST.')
    plan = {'version': 1, 'target': TARGET, 'spreadsheet_id': source['spreadsheet_id'], 'snapshot_at': source['snapshot_at'], 'before': before,
            'after': desired, 'guard': live['guard'], 'changes': changes}
    plan['sha256'] = digest(plan)
    return plan


def validate_plan(plan):
    if plan.get('version') != 1 or plan.get('target') != TARGET or plan.get('sha256') != digest({k: v for k, v in plan.items() if k != 'sha256'}):
        raise RuntimeError('Rencana edit rusak/berubah.')
    before, after = plan['before'], plan['after']
    expected_keys = {'description', *FIELDS.values()}
    if set(before) != expected_keys or set(after) != expected_keys:
        raise RuntimeError('Field rencana tidak sesuai.')
    if any(before[k] != after[k] for k in expected_keys - set(EDIT_FIELDS.values())):
        raise RuntimeError('Edit di luar slot/port/ONU ditolak.')


def verify_desired(plan, source, desired):
    validate_plan(plan)
    if source['spreadsheet_id'] != plan['spreadsheet_id'] or desired != plan['after']:
        raise RuntimeError('Nilai sumber berubah dari pratinjau; jangan apply rencana lama.')


def execute(client, plan, journal, state=None):
    validate_plan(plan)
    live = read_live(client)
    if live['guard'] != plan['guard']:
        raise RuntimeError('Opsi lain berubah sejak pratinjau; tidak menimpa perubahan manual.')
    if state is not None:
        # Resume is reconciliation only: never repeat an uncertain edit POST.
        if state.get('plan') != plan or state.get('phase') not in ('EDIT_ATTEMPTED', 'VERIFIED'):
            raise RuntimeError('Jurnal edit tidak valid.')
        if live['detail'] != plan['after']:
            raise RuntimeError('Hasil edit belum sesuai; POST tidak diulang. Periksa host/jurnal.')
    else:
        if live['detail'] != plan['before']:
            raise RuntimeError('Nilai Cacti berubah sejak pratinjau; POST dibatalkan.')
        payload = dict(live['payload'])
        for name in EDIT_FIELDS.values():
            field = live['soup'].select_one(f'input[name="{name}"]')
            value = plan['after'][name]
            limit = field.get('maxlength', '3')
            if not value.isdigit() or not limit.isdigit() or len(value) > int(limit):
                raise RuntimeError('Nilai melebihi batas field Cacti.')
            payload[name] = value
        target = urljoin(live['response'].url, live['action'])
        client.require_local_url(target)
        state = {'plan': plan, 'phase': 'EDIT_ATTEMPTED', 'attempted_at': datetime.now(timezone.utc).isoformat()}
        durable(journal, state)  # Commit intent before the only Cacti POST.
        # Response timeout/error leaves intent intact for read-only --resume.
        response = client.session.post(target, data=payload, timeout=30, allow_redirects=False)
        response.raise_for_status()
        result = read_live(client)
        if result['detail'] != plan['after'] or result['guard'] != plan['guard']:
            raise RuntimeError('Read-back belum sesuai; tidak mengulang edit atau rollback otomatis.')
    state.update(phase='VERIFIED', host_id=TARGET['host_id'], verified_at=datetime.now(timezone.utc).isoformat())
    durable(journal, state)
    print('VERIFIED: SID 1165651712, SBS host 8471, header 9127 tetap. Tidak ada create/delete/attach.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--plan', action='store_true')
    group.add_argument('--apply', action='store_true')
    group.add_argument('--resume', action='store_true')
    parser.add_argument('--report', default=str(DEFAULT_PLAN))
    parser.add_argument('--journal', default=str(DEFAULT_JOURNAL))
    parser.add_argument('--env-file', default=str(BASE / '.env'))
    args = parser.parse_args()
    load_dotenv(args.env_file)
    api, client = None, None
    try:
        with single_instance():
            api = HDWork()
            source, desired = load_source(api)
            client = Cacti(TARGET['server'])
            client.login()
            journal = Path(args.journal)
            if args.resume:
                state = json.loads(journal.read_text())
                verify_desired(state['plan'], source, desired)
                execute(client, state['plan'], journal, state)
            elif args.apply:
                if journal.exists():
                    raise RuntimeError('Jurnal sudah ada; gunakan --resume, jangan ulang edit.')
                plan = json.loads(Path(args.report).read_text())
                verify_desired(plan, source, desired)
                # Recheck source/mapping immediately before entering the edit pipeline.
                current, current_desired = load_source(api)
                verify_desired(plan, current, current_desired)
                execute(client, plan, journal)
            else:
                if journal.exists():
                    raise RuntimeError('Jurnal sudah ada; simpan audit, jangan menimpa rencana.')
                plan = make_plan(source, desired, read_live(client))
                current, current_desired = load_source(api)
                verify_desired(plan, current, current_desired)
                durable(args.report, plan)
                print(json.dumps({'target': TARGET, 'changes': plan['changes']}, ensure_ascii=False))
                print(f'Pratinjau saja: {args.report}. Tidak ada POST edit Cacti.')
        return 0
    except Exception as error:
        print(str(error) if isinstance(error, RuntimeError) else type(error).__name__, file=sys.stderr)
        print('Berhenti; jangan hapus jurnal atau ulang POST bila hasil belum pasti.', file=sys.stderr)
        return 1
    finally:
        if client:
            client.close()
        if api:
            api.session.close()


if __name__ == '__main__':
    sys.exit(main())
