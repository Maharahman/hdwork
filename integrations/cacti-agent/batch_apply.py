"""One approved ten-device batch, default read-only preflight. No bulk option."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from dotenv import load_dotenv

from batch_preview import header_dependencies
from compare_devices import FIELDS, fetch_inventory, has_datin_identity, planned_description, validate_customer
from pilot_apply import create_payload, execute_reserved_device, save_journal
from sync_headers import Cacti, HDWork, SERVERS, checked_html, normalize


CONFIG = json.loads((Path(__file__).resolve().parents[2] / 'src/config/datin-cacti-batch.json').read_text())
MANIFEST_FIELDS = ('sid', 'server', 'tree_id', 'institution', 'description', 'desired', 'creation_profile')


def freeze_manifest(rows):
    if len(rows) != CONFIG['limit'] or len({row['sid'] for row in rows}) != CONFIG['limit']:
        raise RuntimeError('Batch harus tepat 10 SID unik.')
    manifest = sorted([{key: row[key] for key in MANIFEST_FIELDS} for row in rows], key=lambda row: (row['server'], row['sid']))
    targets = sorted(CONFIG['targets'], key=lambda row: (row['server'], row['sid']))
    for row, target in zip(manifest, targets):
        if any(row[key] != target[key] for key in target):
            raise RuntimeError('Target di luar 10 SID/header yang disetujui.')
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    if digest != CONFIG['approved_sha256']:
        raise RuntimeError('Konfigurasi berbeda dari manifest yang disetujui; jangan apply.')
    return manifest


def validate_source(manifest, source):
    aliases = {row['alias']: row['name'] for row in source.get('aliases', [])}
    for row in manifest:
        matches = [item for item in source['customers'] if item['sid'] == row['sid'] and normalize(item.get('server')) == row['server']]
        if len(matches) != 1 or any(item['sid'] == row['sid'] for item in source.get('conflicts', [])):
            raise RuntimeError('SID batch tidak tersedia unik pada sumber terbaru.')
        item = matches[0]
        name = normalize(item['customer_name'])
        if (validate_customer(item) or (aliases.get(name) or name) != row['institution']
                or planned_description(item) != row['description']
                or any(str(item[key]) != row['desired'][key] for key in FIELDS)):
            raise RuntimeError('Sumber berubah dari manifest yang disetujui.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preview', default=str(Path(__file__).with_name('batch-10.report.json')))
    parser.add_argument('--env-file', default=str(Path(__file__).with_name('.env')))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    load_dotenv(args.env_file)
    clients, api = {}, None
    try:
        preview = json.loads(Path(args.preview).read_text())
        manifest = freeze_manifest(preview['devices'])
        dependencies = header_dependencies(manifest)
        api = HDWork()
        capabilities = api.request('GET', '/api/datin-cacti/batch-capabilities')
        if capabilities.get('version') != 1 or any(capabilities.get(key) != CONFIG[key] for key in ('batch_id', 'limit', 'approved_sha256')):
            raise RuntimeError('Backend batch belum kompatibel.')
        source = api.request('GET', '/api/datin-cacti/device-source')
        if preview['spreadsheet_id'] != source['spreadsheet_id']:
            raise RuntimeError('Spreadsheet berbeda.')
        validate_source(manifest, source)
        mappings = api.request('GET', '/api/datin-cacti/device-mappings')
        if mappings['spreadsheet_id'] != source['spreadsheet_id'] or any(item['sid'] in {row['sid'] for row in manifest} for item in mappings['mappings']):
            raise RuntimeError('SID batch sudah terpetakan/sumber berbeda; jangan buat ulang.')
        journal = Path(__file__).with_name('batch10-execution.report.json')
        if journal.exists():
            raise RuntimeError('Journal batch sudah ada; tinjau hasil sebelumnya, bukan ulang create.')
        for server in SERVERS:
            client = Cacti(server)
            clients[server] = client
            client.login()
            headers = client.headers()
            inventory = fetch_inventory(client)
            # No header adoption: all four must still be absent before this run.
            for dependency in (item for item in dependencies if item['server'] == server):
                if headers.get(dependency['institution']):
                    raise RuntimeError('Header batch sudah ada; perlu rekonsiliasi, bukan create/adopsi otomatis.')
            if any(has_datin_identity(device['description'], row['sid']) for device in inventory for row in manifest):
                raise RuntimeError('Kandidat bertanda batch sudah ada; create ditolak.')
            response = client.session.get(client.base + '/host.php', params={'action': 'edit', 'host_template_id': -1, 'host_status': -1}, timeout=30)
            checked_html(response)
            for row in (item for item in manifest if item['server'] == server):
                create_payload(response.text, row, row['creation_profile'])
                if Path(__file__).with_name(f'batch10-{server}-{row["sid"]}.report.json').exists():
                    raise RuntimeError('Journal SID batch sudah ada; jangan ulang create.')
        current = api.request('GET', '/api/datin-cacti/device-source')
        validate_source(manifest, current)
        if not args.apply:
            print('Preflight batch lolos: 10 device, 4 header. Tidak menulis reservasi/header/device/mapping.')
            return 0
        # Atomic reservation of all SIDs and headers before any Cacti write.
        reservation = api.request('POST', '/api/datin-cacti/batch-reservations', {
            'batch_id': CONFIG['batch_id'], 'spreadsheet_id': current['spreadsheet_id'], 'snapshot_at': current['snapshot_at'], 'manifest': manifest})
        state = {'reservation': reservation, 'phase': 'RESERVED', 'headers': [], 'devices': []}
        save_journal(journal, state, exclusive=True)
        for dependency in dependencies:
            validate_source(manifest, api.request('GET', '/api/datin-cacti/device-source'))
            client = clients[dependency['server']]
            # Recheck after reservation; a concurrent/manual header is not adopted.
            if client.headers().get(dependency['institution']):
                raise RuntimeError('Header muncul setelah preflight; hentikan untuk pemeriksaan.')
            state['phase'] = 'HEADER_CREATE_ATTEMPTED'
            state['current_header'] = {key: dependency[key] for key in ('server', 'institution')}
            save_journal(journal, state)
            header = client.ensure_header(dependency['institution'], apply=True)
            if header.get('action') == 'existing':
                raise RuntimeError('Header dibuat pihak lain; jangan diadopsi otomatis.')
            state['headers'].append(header)
            state['phase'] = 'HEADER_READ_BACK'
            save_journal(journal, state)
            api.request('POST', '/api/datin-cacti/batch-headers', {
                'batch_id': CONFIG['batch_id'], 'token': reservation['token'], 'server': client.server,
                'tree_id': client.tree, 'institution': dependency['institution'], 'header_id': header['header_id']})
            print(f"{client.server}: header {dependency['institution']} terverifikasi, ID {header['header_id']}.", flush=True)
        for row in manifest:
            validate_source(manifest, api.request('GET', '/api/datin-cacti/device-source'))
            client = clients[row['server']]
            header = next(item for item in state['headers'] if item['server'] == row['server'] and item['name'] == row['institution'])
            live_headers = client.headers().get(row['institution'], [])
            if len(live_headers) != 1 or live_headers[0]['header_id'] != header['header_id']:
                raise RuntimeError('Header berubah sebelum create device.')
            if any(has_datin_identity(item['description'], row['sid']) for item in fetch_inventory(client)):
                raise RuntimeError('Device bertanda sudah muncul; jangan ulang create.')
            job = next(item for item in reservation['jobs'] if item['sid'] == row['sid'] and item['server'] == row['server'])
            device_journal = Path(__file__).with_name(f'batch10-{row["server"]}-{row["sid"]}.report.json')
            host_id = execute_reserved_device(client, {**row, 'header_id': header['header_id']}, device_journal, job, api)
            state['devices'].append({'sid': row['sid'], 'server': row['server'], 'host_id': host_id})
            save_journal(journal, state)
            print(f"{client.server}: SID {row['sid']} terverifikasi, host ID {host_id}, mapping tersimpan.", flush=True)
        api.request('POST', '/api/datin-cacti/batch-completions', {'batch_id': CONFIG['batch_id'], 'token': reservation['token']})
        state['phase'] = 'VERIFIED'
        save_journal(journal, state)
        print('Batch 10 device dan 4 header terverifikasi.')
        return 0
    except Exception as error:
        print(str(error) if isinstance(error, RuntimeError) else type(error).__name__, file=sys.stderr)
        print('Berhenti. Jangan hapus journal/reservasi atau ulang POST jika hasil belum pasti.', file=sys.stderr)
        return 1
    finally:
        for client in clients.values():
            client.close()
        if api:
            api.session.close()


if __name__ == '__main__':
    sys.exit(main())
