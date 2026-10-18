"""Create-only DATIN bulk job. Default: read-only plan; --apply/--resume explicit."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
import uuid
from urllib.parse import urljoin

from dotenv import load_dotenv
from batch_apply import validate_source
from compare_devices import compare, fetch_inventory, has_datin_identity, parse_tree_memberships
from device_plan import build_device_plan
from pilot_apply import attach_payload, create_payload, verify_host
from pilot_preview import creation_profile
from sync_headers import Cacti, HDWork, SERVERS, checked_html, normalize, single_instance

FIELDS = ('sid', 'server', 'tree_id', 'institution', 'header_id', 'description', 'desired', 'creation_profile')
BASE = Path(__file__).resolve().parent


def digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def durable(path, data):
    """Atomic, fsynced, private journal: no truncated token/progress after a crash."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def header_entries(client, institution, aliases, headers=None):
    return [entry for name, entries in (client.headers() if headers is None else headers).items()
            if aliases.get(normalize(name), normalize(name)) == institution for entry in entries]


def select_rows(plan, forms, limit=None):
    rows = []
    for row in sorted(plan['rows'], key=lambda r: (r.get('server') or '', r.get('sid') or '')):
        actions = set(row['actions'])
        if (row.get('reasons') or row.get('host_id') or row.get('institution') == 'ATCS'
                or actions not in ({'CREATE_DEVICE', 'ATTACH_TO_TREE'}, {'CREATE_HEADER', 'CREATE_DEVICE', 'ATTACH_TO_TREE'})):
            continue
        # A form failure holds the whole preview, not an unnoticed partial bulk.
        rows.append({**{key: row[key] for key in FIELDS if key != 'creation_profile'},
                     'creation_profile': creation_profile(forms[row['server']], row)})
    if limit is not None:
        rows = rows[:limit]
    if not rows or len(rows) > 1000 or len({r['sid'] for r in rows}) != len(rows):
        raise RuntimeError('Rencana wajib 1–1000 SID unik yang layak create.')
    return rows


def make_plan(api, clients, limit):
    source = api.request('GET', '/api/datin-cacti/device-source')
    mappings = api.request('GET', '/api/datin-cacti/device-mappings')
    if (source['spreadsheet_id'], source['snapshot_at']) != (mappings['spreadsheet_id'], mappings['snapshot_at']):
        raise RuntimeError('Snapshot mapping/sumber berbeda.')
    inventory, headers, forms = [], {}, {}
    for server, client in clients.items():
        inventory.extend(fetch_inventory(client))
        headers[server] = client.headers()
        response = client.session.get(client.base + '/host.php', params={'action': 'edit', 'host_template_id': -1, 'host_status': -1}, timeout=30)
        checked_html(response)
        forms[server] = response.text
    aliases = {r['alias']: r['name'] for r in source.get('aliases', [])}
    comparison = compare(source['customers'], inventory, aliases, source_conflicts=source.get('conflicts'))
    plan = build_device_plan(comparison, mappings['mappings'], headers, inventory, SERVERS, aliases)
    rows = select_rows(plan, forms, limit)
    current = api.request('GET', '/api/datin-cacti/device-source')
    validate_source(rows, current)
    if current['spreadsheet_id'] != source['spreadsheet_id'] or api.request('GET', '/api/datin-cacti/device-mappings')['mappings'] != mappings['mappings']:
        raise RuntimeError('Sumber/mapping berubah saat scan.')
    dependencies = {(r['server'], r['institution']) for r in rows if r['header_id'] is None}
    return {'version': 1, 'spreadsheet_id': source['spreadsheet_id'], 'snapshot_at': current['snapshot_at'],
            'manifest': rows, 'manifest_sha256': digest(rows),
            'summary': {'CREATE_DEVICE': len(rows), 'CREATE_HEADER_UNIQUE': len(dependencies),
                        'by_server': dict(Counter(r['server'] for r in rows)),
                        'excluded': dict(Counter(a for r in plan['rows'] if 'CREATE_DEVICE' not in r['actions'] for a in r['actions']))}}


def validate_plan(plan):
    rows = plan.get('manifest', [])
    if (plan.get('version') != 1 or not rows or len(rows) > 1000
            or len({r['sid'] for r in rows}) != len(rows) or digest(rows) != plan.get('manifest_sha256')):
        raise RuntimeError('Rencana bulk rusak/berubah.')
    for row in rows:
        if (row['server'] not in SERVERS or row['tree_id'] != SERVERS[row['server']]['tree_id']
                or row['institution'] == 'ATCS' or not row['description'].startswith('[HD-D] ' + row['sid'] + '_')):
            raise RuntimeError('Target bukan create DATIN yang diizinkan.')
    return rows


def preflight(api, clients, plan):
    rows = validate_plan(plan)
    source = api.request('GET', '/api/datin-cacti/device-source')
    if source['spreadsheet_id'] != plan['spreadsheet_id']:
        raise RuntimeError('Spreadsheet berbeda.')
    validate_source(rows, source)
    mapped = api.request('GET', '/api/datin-cacti/device-mappings')
    if mapped['spreadsheet_id'] != plan['spreadsheet_id'] or any(m['sid'] in {r['sid'] for r in rows} for m in mapped['mappings']):
        raise RuntimeError('Target sudah terpetakan; buat pratinjau baru.')
    aliases = {r['alias']: r['name'] for r in source.get('aliases', [])}
    for server, client in clients.items():
        inventory = fetch_inventory(client)
        headers = client.headers()
        response = client.session.get(client.base + '/host.php', params={'action': 'edit', 'host_template_id': -1, 'host_status': -1}, timeout=30)
        checked_html(response)
        for r in (r for r in rows if r['server'] == server):
            if any(has_datin_identity(d['description'], r['sid']) for d in inventory):
                raise RuntimeError('Device bertanda sudah ada; jangan membuat duplikat.')
            entries = header_entries(client, r['institution'], aliases, headers)
            if (r['header_id'] is None and entries) or (r['header_id'] is not None and (len(entries) != 1 or entries[0]['header_id'] != r['header_id'])):
                raise RuntimeError('Header berubah dari pratinjau.')
            create_payload(response.text, r, r['creation_profile'])
    return source


def post_local(client, response, action, payload, delay):
    target = urljoin(response.url, action)
    client.require_local_url(target)
    time.sleep(delay)
    # Never retry a Cacti POST; reconcile on an explicit resume.
    client.session.post(target, data=payload, timeout=30, allow_redirects=False).raise_for_status()


def reconcile_device(client, row, state, save, api, job, delay):
    matches = [d for d in fetch_inventory(client) if has_datin_identity(d['description'], row['sid'])]
    if not matches:
        if state['phase'] != 'RESERVED':
            raise RuntimeError('Create pernah dicoba tetapi host tidak ditemukan; perlu pemeriksaan, POST tidak diulang.')
        response = client.session.get(client.base + '/host.php', params={'action': 'edit', 'host_template_id': -1, 'host_status': -1}, timeout=30)
        checked_html(response)
        action, payload = create_payload(response.text, row, row['creation_profile'])
        state['phase'] = 'CREATE_ATTEMPTED'
        save()
        post_local(client, response, action, payload, delay)
        matches = [d for d in fetch_inventory(client) if has_datin_identity(d['description'], row['sid'])]
    elif state['phase'] == 'RESERVED':
        raise RuntimeError('Host muncul sebelum create milik job ini; jangan adopsi.')
    if len(matches) != 1:
        raise RuntimeError('Host hasil create belum unik; pemeriksaan diperlukan.')
    host_id = matches[0]['host_id']
    if state.get('host_id', host_id) != host_id:
        raise RuntimeError('Host ID berbeda dari jurnal.')
    verify_host(client, host_id, row)
    state['host_id'] = host_id
    if state['phase'] == 'CREATE_ATTEMPTED':
        state['phase'] = 'HOST_READ_BACK'
    save()
    response = client.session.get(client.base + '/tree.php', params={'action': 'edit', 'id': client.tree}, timeout=30)
    checked_html(response)
    memberships = parse_tree_memberships(response.text, client.tree).get(host_id, [])
    if not memberships:
        if state['phase'] != 'HOST_READ_BACK':
            raise RuntimeError('Attach pernah dicoba; hasil belum pasti, jangan ulang otomatis.')
        response = client.session.get(client.base + '/tree.php', params={'action': 'item_edit', 'tree_id': client.tree, 'parent_id': row['header_id'], 'type_select': 3}, timeout=30)
        action, payload = attach_payload(checked_html(response), row, host_id)
        state['phase'] = 'ATTACH_ATTEMPTED'
        save()
        post_local(client, response, action, payload, delay)
        response = client.session.get(client.base + '/tree.php', params={'action': 'edit', 'id': client.tree}, timeout=30)
        checked_html(response)
        memberships = parse_tree_memberships(response.text, client.tree).get(host_id, [])
    if len(memberships) != 1 or memberships[0]['header_id'] != row['header_id']:
        raise RuntimeError('Keanggotaan tree berbeda/ganda; tidak diubah.')
    receipt = verify_host(client, host_id, row)
    state.update(phase='TREE_READ_BACK', receipt=receipt)
    save()
    api.request('POST', '/api/datin-cacti/pilot-completions', {'job_id': job['job_id'], 'token': job['token'], 'host_id': host_id, 'receipt': receipt})
    state['phase'] = 'VERIFIED'
    save()


def execute(api, clients, state, journal, delay):
    save = lambda: durable(journal, state)
    plan = state['plan']
    rows = validate_plan(plan)
    source = api.request('GET', '/api/datin-cacti/device-source')
    if source['spreadsheet_id'] != plan['spreadsheet_id']:
        raise RuntimeError('Spreadsheet berubah.')
    validate_source(rows, source)
    aliases = {r['alias']: r['name'] for r in source.get('aliases', [])}
    # Safe identical replay retrieves job tokens even if the first response was lost.
    reservation = api.request('POST', '/api/datin-cacti/bulk-reservations', {
        **{k: plan[k] for k in ('spreadsheet_id', 'manifest', 'manifest_sha256')},
        'snapshot_at': source['snapshot_at'], 'batch_id': state['batch_id'], 'token': state['token']})
    for row in rows:
        current = api.request('GET', '/api/datin-cacti/device-source')
        if current['spreadsheet_id'] != plan['spreadsheet_id']:
            raise RuntimeError('Spreadsheet berubah.')
        validate_source([row], current)
        client = clients[row['server']]
        key = row['server'] + ':' + row['institution']
        header = state['headers'].setdefault(key, {'phase': 'RESERVED', 'header_id': row['header_id']})
        entries = header_entries(client, row['institution'], aliases)
        if header['header_id'] is None:
            if header['phase'] != 'RESERVED' or entries:
                raise RuntimeError('Hasil create header belum pasti; tinjau header/jurnal, jangan ulang POST.')
            header['phase'] = 'CREATE_ATTEMPTED'
            save()
            time.sleep(delay)
            created = client.ensure_header(row['institution'], apply=True)
            if created.get('action') == 'existing':
                raise RuntimeError('Header muncul dari pihak lain; tidak diadopsi.')
            header.update(phase='READ_BACK', header_id=created['header_id'])
            save()
            entries = header_entries(client, row['institution'], aliases)
        if len(entries) != 1 or entries[0]['header_id'] != header['header_id']:
            raise RuntimeError('Header tidak sesuai/ganda; berhenti.')
        if row['header_id'] is None:
            api.request('POST', '/api/datin-cacti/batch-headers', {'batch_id': state['batch_id'], 'token': state['token'],
                'server': row['server'], 'tree_id': row['tree_id'], 'institution': row['institution'], 'header_id': header['header_id']})
        job = next(j for j in reservation['jobs'] if j['sid'] == row['sid'] and j['server'] == row['server'])
        progress = state['devices'].setdefault(row['sid'], {'phase': 'RESERVED'})
        save()
        # Read back even VERIFIED devices on resume; never blindly skip local progress.
        reconcile_device(client, {**row, 'header_id': header['header_id']}, progress, save, api, job, delay)
        count = sum(d['phase'] == 'VERIFIED' for d in state['devices'].values())
        print(f"{count}/{len(rows)} {row['server']} SID {row['sid']}: VERIFIED", flush=True)
    api.request('POST', '/api/datin-cacti/batch-completions', {'batch_id': state['batch_id'], 'token': state['token']})
    state['phase'] = 'VERIFIED'
    save()
    print('Seluruh target bulk terverifikasi. Jalankan compare_devices.py --plan untuk audit akhir.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--resume', action='store_true')
    parser.add_argument('--limit', type=int, help='Jumlah maksimum kandidat; default semua')
    parser.add_argument('--delay', type=float, default=2, help='Jeda POST Cacti, minimum 2 detik')
    parser.add_argument('--report', default=str(BASE / 'bulk-plan.report.json'))
    parser.add_argument('--journal', default=str(BASE / 'bulk-execution.report.json'))
    parser.add_argument('--env-file', default=str(BASE / '.env'))
    args = parser.parse_args()
    if not 2 <= args.delay <= 60 or (args.limit is not None and not 1 <= args.limit <= 1000):
        parser.error('Delay wajib 2–60 detik; limit wajib 1–1000.')
    if (args.apply or args.resume) and args.limit is not None:
        parser.error('--limit hanya untuk membuat plan; eksekusi selalu memakai seluruh plan tetap.')
    load_dotenv(args.env_file)
    api, clients = None, {}
    try:
        with single_instance():
            api = HDWork()
            capabilities = api.request('GET', '/api/datin-cacti/bulk-capabilities')
            if capabilities.get('version') != 1 or not capabilities.get('create_only') or not capabilities.get('resumable'):
                raise RuntimeError('Backend bulk belum kompatibel; deploy/restart dahulu.')
            for server in SERVERS:
                clients[server] = Cacti(server)
                clients[server].login()
            journal = Path(args.journal)
            if not args.apply and not args.resume:
                if journal.exists():
                    raise RuntimeError('Jurnal sudah ada; gunakan --resume, jangan mengganti rencana pekerjaan berjalan.')
                plan = make_plan(api, clients, args.limit)
                durable(args.report, plan)
                print(json.dumps(plan['summary'], ensure_ascii=False))
                print(f'Pratinjau: {args.report}. Tidak ada penulisan ke Cacti/database.')
                return 0
            if args.resume:
                state = json.loads(journal.read_text())
            else:
                if journal.exists():
                    raise RuntimeError('Jurnal sudah ada; gunakan --resume, jangan hapus progres.')
                plan = json.loads(Path(args.report).read_text())
                current = preflight(api, clients, plan)
                state = {'plan': plan, 'batch_id': 'bulk-' + str(uuid.uuid4()), 'token': secrets.token_hex(32),
                         'snapshot_at': current['snapshot_at'], 'phase': 'RESERVED', 'headers': {}, 'devices': {}}
                durable(journal, state)
            execute(api, clients, state, journal, args.delay)
            return 0
    except Exception as error:
        print(str(error) if isinstance(error, RuntimeError) else type(error).__name__, file=sys.stderr)
        print('Berhenti. Simpan jurnal privat; jangan hapus reservasi atau ulang POST Cacti.', file=sys.stderr)
        return 1
    finally:
        for client in clients.values():
            client.close()
        if api:
            api.session.close()


if __name__ == '__main__':
    sys.exit(main())
