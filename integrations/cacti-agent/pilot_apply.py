"""Create only the two approved pilots. Default is a read-only preflight."""
import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import urljoin

from dotenv import load_dotenv

from compare_devices import FIELDS, fetch_inventory, has_datin_identity, parse_device_detail, parse_tree_memberships
from pilot_preview import creation_profile, selected_value
from sync_headers import Cacti, HDWork, SERVERS, checked_html, form_payload


TARGETS = {'SBS': {'sid': '1165651712', 'header_id': 9127}, 'SBU': {'sid': '1015518858', 'header_id': 2929}}


def save_journal(path, state, exclusive=False):
    # Token/receipt survive interruption; never print the reservation token.
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(state, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())


def create_payload(html, row, approved_profile):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    profile = creation_profile(html, row)
    if profile != approved_profile:
        raise RuntimeError('Profil live berbeda dari pratinjau; tidak membuat device.')
    forms = soup.select('form[action]')
    if len(forms) != 1:
        raise RuntimeError('Form create ganda/tidak dikenali.')
    payload = form_payload(soup)  # Legacy Cacti controls extend outside <form>.
    for field in soup.select('textarea[name]'):
        payload[field['name']] = field.get_text()
    for field in soup.select('input[type="checkbox"][name][checked]'):
        payload[field['name']] = field.get('value', 'on')
    payload.update({key: profile[key] for key in ('description', *FIELDS.values(), 'host_template_id', 'snmp_version', 'availability_method', 'ping_method', 'ping_timeout', 'ping_retries', 'monitor_text')})
    payload['monitor'] = 'on'
    payload.pop('disabled', None)
    return forms[0]['action'], payload


def attach_payload(soup, row, host_id):
    for name, expected in {'graph_tree_id': str(row['tree_id']), 'type': '3', 'id': '0', 'action': 'save'}.items():
        fields = soup.select(f'input[name="{name}"]')
        if len(fields) != 1 or fields[0].get('value') != expected:
            raise RuntimeError('Identitas form attach tidak sesuai.')
    if selected_value(soup, 'parent_item_id') != str(row['header_id']):
        raise RuntimeError('Parent header berubah; attach ditahan.')
    hosts = soup.select('select[name="host_id"]')
    if len(hosts) != 1 or not hosts[0].select_one(f'option[value="{host_id}"]'):
        raise RuntimeError('Host baru tidak tersedia pada form tree.')
    forms = soup.select('form[action]')
    if len(forms) != 1:
        raise RuntimeError('Form tree tidak dikenali.')
    payload = form_payload(soup)
    payload.update(host_id=str(host_id), parent_item_id=str(row['header_id']), host_grouping_type='1')
    return forms[0]['action'], payload


def verify_host(client, host_id, row):
    response = client.session.get(client.base + '/host.php', params={'action': 'edit', 'id': host_id}, timeout=30)
    soup = checked_html(response)
    detail = parse_device_detail(response.text, host_id)
    expected = {'description': row['description'], **{target: str(row['desired'][source]) for source, target in FIELDS.items()}}
    if any(detail[key] != value for key, value in expected.items()):
        raise RuntimeError('Read-back konfigurasi tidak sesuai; jangan ulang create.')
    monitor, disabled = soup.select('input[name="monitor"]'), soup.select('input[name="disabled"]')
    if len(monitor) != 1 or not monitor[0].has_attr('checked') or len(disabled) != 1 or disabled[0].has_attr('checked'):
        raise RuntimeError('Read-back monitoring tidak sesuai.')
    receipt = {**row['desired'], 'description': row['description'], 'server': client.server,
               'host_id': host_id, 'tree_id': client.tree, 'header_id': row['header_id'], 'monitor': True, 'disabled': False}
    for name, expected_value in {'host_template_id': '0', 'availability_method': '3', 'ping_method': '1', 'snmp_version': '0'}.items():
        receipt[name] = selected_value(soup, name)
        if receipt[name] != expected_value:
            raise RuntimeError('Read-back opsi monitoring berbeda.')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preview', default=str(Path(__file__).with_name('pilot.report.json')))
    parser.add_argument('--env-file', default=str(Path(__file__).with_name('.env')))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    load_dotenv(args.env_file)
    api, clients = None, []
    try:
        preview = json.loads(Path(args.preview).read_text(encoding='utf-8'))
        rows = preview.get('pilots', [])
        if len(rows) != 2 or {row.get('server') for row in rows} != set(TARGETS):
            raise RuntimeError('Pratinjau harus tepat dua pilot SBS/SBU.')
        api = HDWork()
        # A missing endpoint prevents all Cacti writes, not a mapping-free fallback.
        capabilities = api.request('GET', '/api/datin-cacti/pilot-capabilities')
        if capabilities.get('version') != 1 or capabilities.get('bulk_enabled') is not False:
            raise RuntimeError('Backend pilot tidak kompatibel.')
        source = api.request('GET', '/api/datin-cacti/device-source')
        mappings = api.request('GET', '/api/datin-cacti/device-mappings')
        if preview['spreadsheet_id'] != source['spreadsheet_id'] or mappings['spreadsheet_id'] != source['spreadsheet_id']:
            raise RuntimeError('Sumber spreadsheet berbeda.')
        # Preflight both servers before reserving/creating either pilot.
        prepared = []
        for row in rows:
            server = row['server']
            target = TARGETS[server]
            if (row['sid'], row['header_id'], row['tree_id']) != (target['sid'], target['header_id'], SERVERS[server]['tree_id']):
                raise RuntimeError('Target di luar persetujuan pilot.')
            live_rows = [x for x in source['customers'] if x['sid'] == row['sid'] and x['server'].upper() == server]
            if len(live_rows) != 1 or any(x['sid'] == row['sid'] for x in source.get('conflicts', [])):
                raise RuntimeError('SID tidak tersedia unik pada snapshot terbaru.')
            from compare_devices import planned_description, validate_customer
            live = live_rows[0]
            if validate_customer(live) or planned_description(live) != row['description'] or any(str(live[k]) != str(row['desired'][k]) for k in FIELDS):
                raise RuntimeError('Konfigurasi sumber berubah dari pratinjau yang disetujui.')
            if any(x['sid'] == row['sid'] for x in mappings['mappings']):
                raise RuntimeError('Pilot sudah terpetakan; jangan buat ulang.')
            client = Cacti(server)
            clients.append(client)
            client.login()
            headers = client.headers().get('BANK JATIM', [])
            if len(headers) != 1 or headers[0]['header_id'] != row['header_id']:
                raise RuntimeError('Header pilot berubah/ganda.')
            if any(has_datin_identity(d['description'], row['sid']) for d in fetch_inventory(client)):
                raise RuntimeError('Device bertanda sudah ada; perlu rekonsiliasi, bukan create.')
            response = client.session.get(client.base + '/host.php', params={'action': 'edit', 'host_template_id': -1, 'host_status': -1}, timeout=30)
            checked_html(response)
            create_payload(response.text, row, row['creation_profile'])
            journal = Path(__file__).with_name(f'pilot-{server}.report.json')
            if journal.exists():
                raise RuntimeError(f'Journal {server} sudah ada; periksa hasil sebelumnya, jangan ulang create.')
            prepared.append((client, row, journal))
        if not args.apply:
            print('Preflight dua pilot lolos. PREVIEW ONLY; tidak ada reservasi/create/mapping yang ditulis.')
            return 0
        for client, row, journal in prepared:
            # Acquire a server-side unique reservation before the first Cacti POST.
            current = api.request('GET', '/api/datin-cacti/device-source')
            desired = {**row['desired'], 'description': row['description']}
            job = api.request('POST', '/api/datin-cacti/pilot-reservations', {
                'spreadsheet_id': current['spreadsheet_id'], 'snapshot_at': current['snapshot_at'],
                'sid': row['sid'], 'server': client.server, 'tree_id': row['tree_id'], 'header_id': row['header_id'], 'desired': desired})
            state = {'job': job, 'phase': 'RESERVED'}
            save_journal(journal, state, exclusive=True)
            # Refresh CSRF/form after reservation. Never retry any Cacti creation POST.
            response = client.session.get(client.base + '/host.php', params={'action': 'edit', 'host_template_id': -1, 'host_status': -1}, timeout=30)
            checked_html(response)
            action, payload = create_payload(response.text, row, row['creation_profile'])
            target = urljoin(response.url, action)
            client.require_local_url(target)
            state['phase'] = 'CREATE_ATTEMPTED'
            save_journal(journal, state)
            client.session.post(target, data=payload, timeout=30, allow_redirects=False).raise_for_status()
            matches = [d for d in fetch_inventory(client) if has_datin_identity(d['description'], row['sid'])]
            if len(matches) != 1:
                raise RuntimeError('Create belum terverifikasi unik; jangan ulang POST.')
            host_id = matches[0]['host_id']
            state.update(host_id=host_id, phase='HOST_READ_BACK')
            save_journal(journal, state)
            receipt = verify_host(client, host_id, row)
            response = client.session.get(client.base + '/tree.php', params={'action': 'item_edit', 'tree_id': client.tree, 'parent_id': row['header_id'], 'type_select': 3}, timeout=30)
            soup = checked_html(response)
            action, payload = attach_payload(soup, row, host_id)
            target = urljoin(response.url, action)
            client.require_local_url(target)
            state['phase'] = 'ATTACH_ATTEMPTED'
            save_journal(journal, state)
            client.session.post(target, data=payload, timeout=30, allow_redirects=False).raise_for_status()
            response = client.session.get(client.base + '/tree.php', params={'action': 'edit', 'id': client.tree}, timeout=30)
            checked_html(response)
            memberships = parse_tree_memberships(response.text, client.tree).get(host_id, [])
            if len(memberships) != 1 or memberships[0]['header_id'] != row['header_id']:
                raise RuntimeError('Attach belum terverifikasi; jangan ulang otomatis.')
            receipt = verify_host(client, host_id, row)
            state.update(phase='TREE_READ_BACK', receipt=receipt)
            save_journal(journal, state)
            api.request('POST', '/api/datin-cacti/pilot-completions', {'job_id': job['job_id'], 'token': job['token'], 'host_id': host_id, 'receipt': receipt})
            state['phase'] = 'VERIFIED'
            save_journal(journal, state)
            print(f'{client.server}: pilot {row["sid"]} terverifikasi, host ID {host_id}, mapping tersimpan.')
        return 0
    except Exception as error:
        print(str(error) if isinstance(error, RuntimeError) else type(error).__name__, file=sys.stderr)
        print('Berhenti. Jangan ulang create jika ada reservasi/journal; periksa hasil sebelumnya.', file=sys.stderr)
        return 1
    finally:
        for client in clients:
            client.close()
        if api:
            api.session.close()


if __name__ == '__main__':
    sys.exit(main())
