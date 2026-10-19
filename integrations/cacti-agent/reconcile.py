"""DATIN create/edit/NOT-delete reconciliation. Default read-only; --apply explicit.

--watch --apply runs one non-overlapping cycle every 10 minutes after completion.
Local write-intent journals and backend run/job records survive interrupted calls.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sys
import time
import uuid

from dotenv import load_dotenv
from bulk_sync import durable
from compare_devices import fetch_inventory, has_datin_identity, parse_tree_memberships, planned_description, validate_customer
from pilot_apply import attach_payload, create_payload
from pilot_preview import creation_profile
from reconcile_forms import read_host, edit_payload, device_actions, references, require_exclusive, delete_confirmation
from sync_headers import Cacti, HDWork, SERVERS, checked_html, normalize, single_instance

BASE = Path(__file__).resolve().parent


class AgentError(RuntimeError):
    def __init__(self, status, message, safe_to_replan=False):
        super().__init__(f'HD Work HTTP {status}: {message}')
        self.status, self.safe_to_replan = status, safe_to_replan


class AgentAPI(HDWork):
    def request(self, method, path, payload=None):
        response = self.session.request(method, self.base + path, json=payload, timeout=30, allow_redirects=False)
        try:
            data = response.json()
        except ValueError:
            raise AgentError(response.status_code, 'Respons bukan JSON; periksa proxy/backend.')
        if not 200 <= response.status_code < 300 or not data.get('success'):
            raise AgentError(response.status_code, str(data.get('message', 'Request gagal'))[:400], data.get('safe_to_replan') is True)
        return data


def canonical(source, name):
    name = normalize(name)
    return next((r['name'] for r in source['aliases'] if r['alias'] == name), name)


def identity(mapping):
    return {k: mapping[k] for k in ('sid', 'server', 'tree_id', 'header_id', 'host_id', 'sync_state', 'ownership_status')} if mapping else None


def desired(row):
    if validate_customer(row) or normalize(row.get('status')) != 'OK':
        raise RuntimeError('Konfigurasi sumber tidak lengkap/valid.')
    result = {k: str(row[k]).strip() for k in ('olt_ip', 'slot', 'port', 'onu', 'serial_number')}
    for k in ('slot', 'port', 'onu'):
        if not result[k].isdigit() or len(result[k]) > 3:
            raise RuntimeError('Slot/port/ONU wajib 0–999; nol diperbolehkan.')
        result[k] = str(int(result[k]))
    result['description'] = planned_description(row)
    if len(result['description']) > 250 or len(result['serial_number']) > 50:
        raise RuntimeError('Nama/SN melebihi batas form Cacti.')
    return result


def header_entries(source, client, institution, headers=None):
    return [h for name, entries in (client.headers() if headers is None else headers).items()
            if canonical(source, name) == institution for h in entries]


def unique_status(source, sid):
    rows = [r for r in source['inventory']['rows'] if normalize(r['sid']) == normalize(sid)]
    if len(rows) != 1 or rows[0]['sid'] != sid:
        raise RuntimeError('SID hilang/konflik; bukan bukti NOT.')
    return rows[0]


def check_current(source, job):
    """Semantic checks: a new identical 10-minute snapshot does not invalidate work."""
    p, e = job['request'], job['expected']
    if p['operation'] == 'HEADER':
        if not any(normalize(r['server']) == e['server'] and canonical(source, r['customer_name']) == e['institution'] for r in source['customers']):
            raise RuntimeError('Instansi header tidak lagi OK.')
        return
    row = unique_status(source, p['sid'])
    mappings = [m for m in source['mappings'] if m['sid'] == p['sid']]
    current_mapping = identity(mappings[0]) if len(mappings) == 1 else None
    if len(mappings) > 1 or current_mapping != e.get('mapping'):
        raise RuntimeError('Mapping berubah sebelum write.')
    if p['operation'] in ('CREATE', 'UPDATE', 'VERIFY'):
        matches = [r for r in source['customers'] if r['sid'] == p['sid']]
        if row['status'] != 'OK' or len(matches) != 1 or desired(matches[0]) != e['config'] or row['server'] != e['server'] or canonical(source, row['customer_name']) != e['institution']:
            raise RuntimeError('Sumber berubah sebelum write; jangan gunakan konfigurasi lama.')
    else:
        if (row['status'] != 'NOT' or not source['inventory']['safe_for_delete']
                or row['server'] != e['row']['server'] or row['customer_name'] != e['row']['customer_name']):
            raise RuntimeError('Bukti NOT berubah/tidak aman.')
        candidates = [m for m in source['mappings'] if m['sync_state'] != 'DELETED' and
                      len([r for r in source['inventory']['rows'] if normalize(r['sid']) == normalize(m['sid'])]) == 1 and
                      any(r['sid'] == m['sid'] and r['status'] == 'NOT' for r in source['inventory']['rows'])]
        if len(candidates) > source.get('max_deletes_per_run', 10):
            raise RuntimeError('Jumlah delete terbaru melebihi batas; hentikan untuk review.')


def plan_rows(source, inventory, clients, full_audit=False, delete_limit=10):
    mappings = {m['sid']: m for m in source['mappings']}
    accepted = {r['sid']: r for r in source['customers']}
    groups = {}
    for row in source['inventory']['rows']:
        if row['sid']:
            groups.setdefault(normalize(row['sid']), []).append(row)
    headers = {s: c.headers() for s, c in clients.items()}
    results = []
    for key in sorted(groups):
        rows = groups[key]
        r = rows[0]
        item = {'sid': r['sid'], 'operation': 'HOLD'}
        try:
            if len(rows) != 1:
                raise RuntimeError('SID duplikat lintas OK/NOT; semua baris ditahan.')
            m = mappings.get(r['sid'])
            institution = canonical(source, r['customer_name'])
            if institution == 'ATCS':
                raise RuntimeError('ATCS dilindungi.')
            if r['status'] not in ('OK', 'NOT'):
                if m:
                    raise RuntimeError('Status bukan OK/NOT; bukan perintah delete.')
                continue
            if r['status'] == 'NOT':
                if not source['inventory']['safe_for_delete']:
                    if m:
                        raise RuntimeError('Snapshot turun drastis/tidak aman untuk delete.')
                    continue
                if not m or m['sync_state'] == 'DELETED':
                    if any(x['sid'] == r['sid'] for x in source['masters']):
                        item.update(operation='CLEANUP')
                    else:
                        continue
                else:
                    if m['ownership_status'] != 'VERIFIED' or not m['verified_at'] or m['server'] != r['server'] or r['server'] not in SERVERS or not institution:
                        raise RuntimeError('Mapping/server NOT tidak sesuai; perlu review.')
                    hs = header_entries(source, clients[m['server']], institution, headers[m['server']])
                    if len(hs) != 1 or hs[0]['header_id'] != m['header_id']:
                        raise RuntimeError('Header NOT berbeda/ganda.')
                    item.update(operation='DELETE', mapping=m)
            else:
                customer = accepted.get(r['sid'])
                if not customer:
                    raise RuntimeError('Baris OK tidak diterima backend; jangan membuat/mengubah.')
                wanted = desired(customer)
                server = normalize(customer['server'])
                if server not in SERVERS:
                    raise RuntimeError('Server tidak dikenal.')
                hs = header_entries(source, clients[server], institution, headers[server])
                if len(hs) > 1:
                    raise RuntimeError('Header instansi ganda.')
                stored_ids = {h['header_id'] for h in source['headers'] if h['server'] == server and canonical(source, h['normalized_name']) == institution}
                if len(stored_ids) > 1 or (stored_ids and (not hs or hs[0]['header_id'] not in stored_ids)):
                    raise RuntimeError('Header mapping hilang/berubah; jangan membuat pengganti.')
                candidates = [h for h in inventory if has_datin_identity(h['description'], r['sid'])]
                item.update(server=server, tree_id=SERVERS[server]['tree_id'], institution=institution, desired=wanted,
                            header_id=hs[0]['header_id'] if hs else None)
                if not m or m['sync_state'] == 'DELETED':
                    if candidates:
                        raise RuntimeError('Host bertanda tanpa mapping aktif; jangan adopsi/create duplikat.')
                    item['operation'] = 'CREATE'
                else:
                    if m['ownership_status'] != 'VERIFIED' or not m['verified_at'] or m['server'] != server or m['tree_id'] != item['tree_id'] or m['header_id'] != item['header_id']:
                        raise RuntimeError('Perubahan server/tree/header ditahan untuk migrasi manual.')
                    if len(candidates) != 1 or candidates[0]['host_id'] != m['host_id'] or candidates[0]['server'] != server:
                        raise RuntimeError('Host mapping hilang/identitas berbeda; tidak membuat pengganti otomatis.')
                    require_exclusive(candidates[0]['memberships'], m['tree_id'], m['header_id'])
                    item['mapping'] = m
                    if not full_audit and m.get('last_applied_json') == wanted and candidates[0]['description'] == wanted['description']:
                        item['operation'] = 'NO_CHANGE'
                    else:
                        live = read_host(clients[server], r['sid'], m['host_id'])
                        item['operation'] = 'VERIFY' if live['config'] == wanted else 'UPDATE'
            results.append(item)
        except RuntimeError as error:
            results.append({'sid': r['sid'], 'operation': 'HOLD', 'reason': str(error)})
    present = set(groups)
    for m in source['mappings']:
        if m['sync_state'] != 'DELETED' and normalize(m['sid']) not in present:
            results.append({'sid': m['sid'], 'operation': 'HOLD', 'reason': 'SID tidak ada di inventaris; bukan bukti NOT.'})
    deletes = [r for r in results if r['operation'] == 'DELETE']
    if len(deletes) > delete_limit:
        for r in deletes:
            r.update(operation='HOLD', reason=f'Kandidat delete {len(deletes)} melebihi batas {delete_limit} per run; periksa sumber.')
    return results


class Reconciler:
    def __init__(self, api, clients, state, journal, delay=2):
        self.api, self.clients, self.state, self.journal, self.delay = api, clients, state, Path(journal), delay

    def save(self):
        durable(self.journal, self.state)

    def call(self, path, payload=None):
        return self.api.request('POST', '/api/datin-cacti/sync/' + path,
                                {'run_id': self.state['run_id'], 'token': self.state['token'], **(payload or {})})

    def source(self):
        source = self.api.request('GET', '/api/datin-cacti/sync/source')
        if source['spreadsheet_id'] != self.state['spreadsheet_id']:
            raise RuntimeError('Spreadsheet berubah.')
        return source

    def inventory(self):
        return [d for c in self.clients.values() for d in fetch_inventory(c)]

    def send(self, client, url, payload, job, marker):
        client.require_local_url(url)
        check_current(self.source(), job)
        job[marker] = True
        self.save()  # Durable intent BEFORE each Cacti mutation.
        time.sleep(self.delay)
        response = client.session.post(url, data=payload, timeout=30, allow_redirects=False)
        response.raise_for_status()
        return response

    def reserve(self, request, extra=None):
        record = {'id': str(uuid.uuid4()), 'request': request, 'local_status': 'NEW', **(extra or {})}
        self.state['jobs'].append(record)
        self.save()  # Even a lost reservation response can be replayed identically.
        return self.process(record)

    def process(self, job):
        if job.get('local_status') in ('VERIFIED', 'CANCELLED'):
            return job.get('receipt')
        try:
            result = self.call('jobs/prepare', {'job_id': job['id'], **job['request']})
        except AgentError as error:
            if error.safe_to_replan and job.get('local_status') == 'NEW':
                job.update(local_status='CANCELLED', reason=str(error)); self.save()
                return None
            raise
        job['expected'] = result['expected']
        if result['state'] in ('VERIFIED', 'CANCELLED'):
            job.update(local_status=result['state'], receipt=result.get('receipt'))
            self.save()
            return job.get('receipt')
        # A saved receipt can complete a lost backend ACK without any Cacti POST.
        if job.get('receipt'):
            self.call('jobs/complete', {'job_id': job['id'], 'receipt': job['receipt']})
            job['local_status'] = 'VERIFIED'
            self.save()
            return job['receipt']
        remote_intent = any(job.get(k) for k in ('header_attempted', 'create_attempted', 'attach_attempted', 'update_attempted', 'delete_attempted'))
        # Once a remote write might have occurred, reconcile its OLD result even
        # if the sheet changed meanwhile. Any additional POST still checks the
        # current source in send(). Never cancel an uncertain remote mutation.
        if result['state'] == 'PREPARED' or not remote_intent:
            try:
                check_current(self.source(), job)
            except RuntimeError:
                self.call('jobs/cancel', {'job_id': job['id'], 'no_cacti_write': not remote_intent, 'journal_proof': 'NO_MUTATION_INTENT'})
                job['local_status'] = 'CANCELLED'
                self.save()
                return None
        if result['state'] == 'PREPARED':
            try:
                self.call('jobs/attempt', {'job_id': job['id']})
            except AgentError as error:
                if error.status == 409:
                    self.call('jobs/cancel', {'job_id': job['id']})
                    job.update(local_status='CANCELLED', reason=str(error)); self.save()
                    return None
                raise
        job['local_status'] = 'ATTEMPTED'
        self.save()
        action = job['request']['operation']
        if action == 'HEADER':
            receipt = self.header(job)
        elif action == 'CREATE':
            receipt = self.create(job)
        elif action in ('UPDATE', 'VERIFY'):
            receipt = self.update(job)
        elif action == 'DELETE':
            receipt = self.delete(job)
        else:
            receipt = {'database_only': True}
        job['receipt'] = receipt
        self.save()
        self.call('jobs/complete', {'job_id': job['id'], 'receipt': receipt})
        job['local_status'] = 'VERIFIED'
        self.save()
        print(f"{action} {job['request'].get('sid') or job['expected'].get('institution')}: VERIFIED", flush=True)
        return receipt

    def header(self, job):
        e = job['expected']; client = self.clients[e['server']]
        source = self.source()
        entries = header_entries(source, client, e['institution'])
        if e['header_id'] is None:
            if not job.get('header_attempted'):
                if entries:
                    raise RuntimeError('Header muncul setelah preflight; tidak diadopsi.')
                check_current(source, job)
                job['header_attempted'] = True; self.save()
                time.sleep(self.delay)
                result = client.ensure_header(e['institution'], apply=True)
                if result['action'] == 'existing':
                    raise RuntimeError('Header dibuat pihak lain; perlu review.')
                job['header_id'] = result['header_id']; self.save()
                entries = header_entries(self.source(), client, e['institution'])
            if len(entries) != 1:
                raise RuntimeError('Hasil create header belum unik/pasti; tidak mengulang POST.')
            if job.get('header_id', entries[0]['header_id']) != entries[0]['header_id']:
                raise RuntimeError('Header hasil berbeda dari jurnal.')
            header_id = entries[0]['header_id']
        else:
            if len(entries) != 1 or entries[0]['header_id'] != e['header_id']:
                raise RuntimeError('Header lama berubah/ganda.')
            header_id = e['header_id']
        return {'server': e['server'], 'tree_id': e['tree_id'], 'institution': e['institution'], 'header_id': header_id}

    def host_receipt(self, client, job, host_id):
        e = job['expected']
        live = read_host(client, job['request']['sid'], host_id)
        if live['config'] != e['config']:
            raise RuntimeError('Read-back konfigurasi belum sesuai; tidak ulang write.')
        refs = references(client, host_id)
        require_exclusive(refs, e['tree_id'], e['header_id'])
        return {'config': live['config'], 'server': e['server'], 'tree_id': e['tree_id'], 'header_id': e['header_id'],
                'host_id': host_id, 'monitor': True, 'disabled': False, 'exclusive_tree': True, 'option_guard': live['option_guard']}

    def create(self, job):
        e = job['expected']; client = self.clients[e['server']]; sid = job['request']['sid']
        matches = [d for d in self.inventory() if has_datin_identity(d['description'], sid)]
        if not job.get('create_attempted'):
            if matches:
                raise RuntimeError('Host bertanda muncul sebelum create; tidak adopsi.')
            response = client.session.get(client.base + '/host.php', params={'action': 'edit', 'host_template_id': -1, 'host_status': -1}, timeout=30)
            checked_html(response)
            row = {'description': e['config']['description'], 'desired': {k: v for k, v in e['config'].items() if k != 'description'}}
            profile = creation_profile(response.text, row)
            action, payload = create_payload(response.text, row, profile)
            from urllib.parse import urljoin
            before = fetch_inventory(client)
            job['before_host_ids'] = [d['host_id'] for d in before]; self.save()
            self.send(client, urljoin(response.url, action), payload, job, 'create_attempted')
            matches = [d for d in self.inventory() if has_datin_identity(d['description'], sid)]
        if len(matches) != 1 or matches[0]['server'] != e['server'] or matches[0]['host_id'] in job['before_host_ids']:
            raise RuntimeError('Create belum terverifikasi unik; POST tidak diulang.')
        host_id = matches[0]['host_id']
        if job.get('host_id', host_id) != host_id:
            raise RuntimeError('Host hasil berbeda dari jurnal.')
        job['host_id'] = host_id; self.save()
        live = read_host(client, sid, host_id)
        if live['config'] != e['config']:
            raise RuntimeError('Host hasil create tidak sesuai konfigurasi.')
        response = client.session.get(client.base + '/tree.php', params={'action': 'edit', 'id': client.tree}, timeout=30)
        checked_html(response)
        members = parse_tree_memberships(response.text, client.tree).get(host_id, [])
        if not members:
            if job.get('attach_attempted'):
                raise RuntimeError('Attach pernah dicoba; hasil belum pasti, jangan ulang POST.')
            response = client.session.get(client.base + '/tree.php', params={'action': 'item_edit', 'tree_id': client.tree, 'parent_id': e['header_id'], 'type_select': 3}, timeout=30)
            action, payload = attach_payload(checked_html(response), {'tree_id': client.tree, 'header_id': e['header_id']}, host_id)
            from urllib.parse import urljoin
            self.send(client, urljoin(response.url, action), payload, job, 'attach_attempted')
        return self.host_receipt(client, job, host_id)

    def update(self, job):
        e = job['expected']; client = self.clients[e['server']]; sid = job['request']['sid']; m = e['mapping']
        live = read_host(client, sid, m['host_id'])
        _, _, ids = device_actions(client, sid, m['host_id'])
        require_exclusive(references(client, m['host_id'], ids), e['tree_id'], e['header_id'])
        observed = job['request']['observed']
        if live['option_guard'] != observed['option_guard']:
            raise RuntimeError('Opsi non-target berubah; tidak ditimpa.')
        if live['config'] != e['config']:
            if job.get('update_attempted') or e['operation'] == 'VERIFY' or live['config'] != observed['config']:
                raise RuntimeError('Hasil edit belum sesuai/host berubah; POST tidak diulang.')
            self.send(client, live['url'], edit_payload(live, e['config']), job, 'update_attempted')
        return self.host_receipt(client, job, m['host_id'])

    def delete(self, job):
        e = job['expected']; m = e['mapping']; client = self.clients[m['server']]; sid = job['request']['sid']
        inventory = fetch_inventory(client)
        ids = {d['host_id'] for d in inventory}
        present = m['host_id'] in ids
        if not job.get('before_host_ids'):
            job['before_host_ids'] = sorted(ids); job['before_headers'] = client.headers(); self.save()
        if present:
            if job.get('delete_attempted'):
                raise RuntimeError('Delete pernah dicoba tetapi host masih ditemukan; tidak ulang POST.')
            live = read_host(client, sid, m['host_id'])
            obs = job['request']['observed']
            if live['config'] != obs['config'] or live['option_guard'] != obs['option_guard']:
                raise RuntimeError('Host berubah sejak preflight delete.')
            url, payload, tree_ids = device_actions(client, sid, m['host_id'])
            require_exclusive(references(client, m['host_id'], tree_ids), m['tree_id'], m['header_id'])
            payload.update(action='actions', drp_action='1', **{f"chk_{m['host_id']}": 'on'})
            response = self.send(client, url, payload, job, 'selection_attempted')
            action, payload = delete_confirmation(response, m['host_id'], live['config']['description'])
            self.send(client, action, payload, job, 'delete_attempted')
            ids = {d['host_id'] for d in fetch_inventory(client)}
        if m['host_id'] in ids:
            raise RuntimeError('Host masih ada setelah delete; jangan ulang POST.')
        if ids != set(job['before_host_ids']) - {m['host_id']}:
            raise RuntimeError('Inventaris host lain berubah; tinjau audit, jangan ulang delete.')
        if client.headers() != job['before_headers']:
            raise RuntimeError('Header berubah saat delete; tinjau audit.')
        require_exclusive(references(client, m['host_id']), m['tree_id'], m['header_id'], absent=True)
        return {'host_id': m['host_id'], 'server': m['server'], 'absent': True, 'inventory_complete': True,
                'other_host_ids_unchanged': True, 'headers_unchanged': True, 'no_tree_references': True}


def execute_item(worker, item):
    source = worker.source()
    sid, action = item['sid'], item['operation']
    status = unique_status(source, sid)
    if action == 'CLEANUP':
        return worker.reserve({'sid': sid, 'operation': action})
    if action == 'CREATE':
        row = next(r for r in source['customers'] if r['sid'] == sid)
        config = desired(row); server = normalize(row['server']); client = worker.clients[server]
        institution = canonical(source, row['customer_name'])
        entries = header_entries(source, client, institution)
        if len(entries) > 1:
            raise RuntimeError('Header ganda.')
        hs = worker.reserve({'operation': 'HEADER', 'observed': {'server': server, 'tree_id': client.tree,
                            'institution': institution, 'header_id': entries[0]['header_id'] if entries else None}})
        if not hs:
            return
        inventory = worker.inventory()
        if any(has_datin_identity(d['description'], sid) for d in inventory):
            raise RuntimeError('Host bertanda sudah muncul; jangan create duplikat.')
        return worker.reserve({'sid': sid, 'operation': action, 'desired': config,
                              'observed': {'absent': True, 'inventory_complete': True, 'header_id': hs['header_id']}})
    mappings = [m for m in source['mappings'] if m['sid'] == sid]
    if len(mappings) != 1:
        raise RuntimeError('Mapping tidak unik.')
    m = mappings[0]; client = worker.clients[m['server']]
    inventory = fetch_inventory(client)
    absent = not any(d['host_id'] == m['host_id'] for d in inventory)
    observed = {'mapping': identity(m), 'header_id': m['header_id'], 'inventory_complete': True, 'absent': absent}
    if not absent:
        live = read_host(client, sid, m['host_id'])
        _, _, ids = device_actions(client, sid, m['host_id'])
        require_exclusive(references(client, m['host_id'], ids), m['tree_id'], m['header_id'])
        observed.update(config=live['config'], option_guard=live['option_guard'], exclusive_tree=True)
    elif action != 'DELETE':
        raise RuntimeError('Host hilang; tidak membuat pengganti.')
    else:
        require_exclusive(references(client, m['host_id']), m['tree_id'], m['header_id'], absent=True)
    request = {'sid': sid, 'operation': action, 'observed': observed}
    if action in ('UPDATE', 'VERIFY'):
        request['desired'] = desired(next(r for r in source['customers'] if r['sid'] == sid))
    return worker.reserve(request)


def cycle(args):
    api, clients = AgentAPI(), {}
    try:
        capabilities = api.request('GET', '/api/datin-cacti/sync/capabilities')
        if capabilities.get('version') != 1 or (args.apply and not capabilities.get('enabled')):
            raise RuntimeError('Backend reconcile belum kompatibel/aktif; deploy, migrasi, dan sinkronkan Apps Script dahulu.')
        source = api.request('GET', '/api/datin-cacti/sync/source')
        for server in SERVERS:
            clients[server] = Cacti(server); clients[server].login()
        journal = Path(args.journal)
        if not args.apply:
            if journal.exists():
                raise RuntimeError('Ada run aktif; jalankan --apply untuk resume jurnal, jangan menimpa rencana.')
            inventory = [d for c in clients.values() for d in fetch_inventory(c)]
            rows = plan_rows(source, inventory, clients, args.full_audit, capabilities['max_deletes_per_run'])
            report = {'snapshot_at': source['snapshot_at'], 'mode': 'PREVIEW_ONLY', 'rows': rows,
                      'summary': dict(Counter(r['operation'] for r in rows))}
            durable(args.report, report)
            print(json.dumps(report['summary'], ensure_ascii=False)); print(f'Pratinjau: {args.report}. Tidak ada penulisan ke Cacti/database.')
            return
        if journal.exists():
            state = json.loads(journal.read_text())
        else:
            state = {'run_id': str(uuid.uuid4()), 'token': secrets.token_hex(32), 'spreadsheet_id': source['spreadsheet_id'],
                     'jobs': [], 'started_at': datetime.now(timezone.utc).isoformat()}
            durable(journal, state)
        worker = Reconciler(api, clients, state, journal, args.delay)
        started = worker.call('runs', {'spreadsheet_id': state['spreadsheet_id']})
        if started['state'] != 'DONE':
            # Recover pending remote calls before considering any new actions.
            for job in state['jobs']:
                worker.process(job)
            source = worker.source()
            inventory = worker.inventory()
            rows = plan_rows(source, inventory, clients, args.full_audit, capabilities['max_deletes_per_run'])
            for item in rows:
                if item['operation'] in ('HOLD', 'NO_CHANGE'):
                    continue
                try:
                    execute_item(worker, item)
                except RuntimeError as error:
                    if any(j.get('local_status') not in ('VERIFIED', 'CANCELLED') for j in state['jobs']):
                        raise
                    item.update(operation='HOLD', reason=str(error))
            summary = dict(Counter(r['operation'] for r in rows))
            state['report'] = {'planned': summary, 'verified': dict(Counter(j['request']['operation'] for j in state['jobs'] if j.get('local_status') == 'VERIFIED')),
                               'cancelled': [j['request'].get('sid') for j in state['jobs'] if j.get('local_status') == 'CANCELLED'],
                               'holds': [r for r in rows if r['operation'] == 'HOLD']}
            worker.save()
            worker.call('runs/complete', {'summary': state['report']})
        state['completed_at'] = datetime.now(timezone.utc).isoformat()
        worker.save()
        archive = journal.with_name(f"reconcile-{state['run_id']}.report.json")
        os.replace(journal, archive)
        print(json.dumps(state.get('report', {'state': 'DONE'}), ensure_ascii=False), flush=True)
        print(f'Run selesai. Audit privat: {archive}', flush=True)
    except Exception as error:
        # Best effort diagnostic only. Never conceal the original failure or
        # overwrite a Cacti write intent if the server is unreachable.
        if args.apply:
            try:
                journal = Path(args.journal)
                if journal.exists():
                    state = json.loads(journal.read_text())
                    message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                    state['last_error'] = message
                    durable(journal, state)
                    api.request('POST', '/api/datin-cacti/sync/runs/error', {'run_id': state['run_id'], 'token': state['token'], 'message': message})
            except Exception:
                pass
        raise
    finally:
        for client in clients.values():
            client.close()
        api.session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--interval', type=int, default=600)
    parser.add_argument('--delay', type=float, default=2)
    parser.add_argument('--full-audit', action='store_true', help='Read every mapped host instead of using the last verified configuration cache')
    parser.add_argument('--env-file', default=str(BASE / '.env'))
    parser.add_argument('--report', default=str(BASE / 'reconcile-plan.report.json'))
    parser.add_argument('--journal', default=str(BASE / 'reconcile-active.report.json'))
    args = parser.parse_args()
    if (args.plan and args.apply) or (args.watch and not args.apply) or args.interval < 600 or not 2 <= args.delay <= 60:
        parser.error('Gunakan --plan atau --apply; --watch wajib --apply; interval minimum 600 detik, delay 2–60.')
    load_dotenv(args.env_file)
    try:
        with single_instance():
            while True:
                try:
                    cycle(args)
                except Exception as error:
                    print(str(error) if isinstance(error, RuntimeError) else type(error).__name__, file=sys.stderr, flush=True)
                    print('Simpan jurnal. Jangan hapus reservasi/ulang POST Cacti. Siklus berikutnya merekonsiliasi progres lama dahulu.', file=sys.stderr, flush=True)
                    if not args.watch:
                        return 1
                if not args.watch:
                    return 0
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print('Agent dihentikan; progres tersimpan untuk resume.')
        return 0
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
