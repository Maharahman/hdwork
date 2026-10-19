"""Manual deletion pilot for SBS host 8471 only; human-confirmed NOT required.

Absence from the OK-only API is NOT proof of NOT. This pilot additionally needs
explicit human evidence. It is not an automatic NOT/deletion reconciler.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import NavigableString, Tag
from dotenv import load_dotenv
from bulk_sync import durable, digest
from compare_devices import fetch_inventory, parse_tree_memberships
from edit_device import TARGET, read_live
from sync_headers import Cacti, HDWork, checked_html, form_payload, single_instance

BASE = Path(__file__).resolve().parent
PLAN = BASE / 'delete-1165651712-plan.report.json'
JOURNAL = BASE / 'delete-1165651712-execution.report.json'


def source_check(api):
    source = api.request('GET', '/api/datin-cacti/device-source')
    mappings = api.request('GET', '/api/datin-cacti/device-mappings')
    if (source['spreadsheet_id'] != mappings['spreadsheet_id']
            or any(r['sid'] == TARGET['sid'] for r in source['customers'])
            or any(r['sid'] == TARGET['sid'] for r in source.get('conflicts', []))):
        raise RuntimeError('SID masih OK/konflik atau sumber berbeda; delete ditolak.')
    rows = [r for r in mappings['mappings'] if r['sid'] == TARGET['sid']]
    if (len(rows) != 1 or any(rows[0].get(k) != v for k, v in TARGET.items())
            or rows[0].get('ownership_status') != 'VERIFIED' or not rows[0].get('verified_at')):
        raise RuntimeError('Mapping bukan host pilot VERIFIED; delete ditolak.')
    return source


def list_form(client):
    response = client.session.get(client.base + '/host.php', params={
        'host_template_id': -1, 'host_status': -1, 'filter': TARGET['sid'], 'host_rows': 30, 'page': 1}, timeout=30)
    soup = checked_html(response)
    forms = [f for f in soup.select('form') if f.select_one('select[name="drp_action"]')]
    if len(forms) != 1:
        raise RuntimeError('Form tindakan device tidak dikenali.')
    form = forms[0]
    delete = form.select_one('select[name="drp_action"] option[value="1"]')
    if not delete or delete.get_text(strip=True) != 'Delete':
        raise RuntimeError('Tindakan Delete tidak dikenali.')
    checks = [x.get('name') for x in form.select('input[type="checkbox"][name]') if x.get('name', '').startswith('chk_')]
    if checks != ['chk_8471']:
        raise RuntimeError('Hasil filter bukan hanya host pilot; delete ditolak.')
    payload = form_payload(form)
    if payload.get('action') != 'actions' or 'selected_items' in payload:
        raise RuntimeError('Identitas form daftar tidak sesuai.')
    tree_ids = {int(o['value'][3:]) for o in form.select('select[name="drp_action"] option[value^="tr_"]') if o['value'][3:].isdigit()}
    return response, form, payload, tree_ids


def tree_audit(client, expected_ids):
    response = client.session.get(client.base + '/tree.php', timeout=30)
    soup = checked_html(response)
    ids = set()
    for a in soup.select('a[href]'):
        url = urlparse(a['href'])
        q = parse_qs(url.query)
        if url.path.split('/')[-1] == 'tree.php' and q.get('action') == ['edit'] and q.get('id', [''])[0].isdigit():
            ids.add(int(q['id'][0]))
    if not ids or ids != expected_ids or TARGET['tree_id'] not in ids:
        raise RuntimeError('Inventaris tree tidak lengkap/berbeda dari menu device.')
    references = []
    for tree_id in sorted(ids):
        response = client.session.get(client.base + '/tree.php', params={'action': 'edit', 'id': tree_id}, timeout=30)
        checked_html(response)
        references.extend(parse_tree_memberships(response.text, tree_id).get(TARGET['host_id'], []))
    if len(references) != 1 or references[0]['tree_id'] != 34 or references[0]['header_id'] != 9127:
        raise RuntimeError('Host dipakai tree lain/header berbeda; tidak boleh dihapus.')
    return sorted(ids)


def make_plan(client, source):
    live = read_live(client)
    if live['detail']['description'] != '[HD-D] 1165651712_BANK JATIM_KF KENJERAN':
        raise RuntimeError('Identitas host pilot berubah.')
    _, _, _, ids = list_form(client)
    trees = tree_audit(client, ids)
    # No credentials, CSRF or raw HTML in the backup. Host can be reconstructed,
    # but deletion has no automatic undo and may require a new host ID.
    plan = {'version': 1, 'target': TARGET, 'spreadsheet_id': source['spreadsheet_id'],
            'snapshot_at': source['snapshot_at'], 'evidence': 'HUMAN_CONFIRMED_NOT',
            'before': live['detail'], 'option_guard': live['guard'], 'visible_trees_checked': trees,
            'preserve_graphs': True, 'retain_hdwork_history': True}
    plan['sha256'] = digest(plan)
    return plan


def validate_plan(plan):
    if (plan.get('version') != 1 or plan.get('target') != TARGET or plan.get('evidence') != 'HUMAN_CONFIRMED_NOT'
            or plan.get('preserve_graphs') is not True or plan.get('retain_hdwork_history') is not True
            or plan.get('sha256') != digest({k: v for k, v in plan.items() if k != 'sha256'})):
        raise RuntimeError('Rencana delete tidak valid/berubah.')


def prepare_confirmation(client, api, plan, journal):
    validate_plan(plan)
    source = source_check(api)
    if source['spreadsheet_id'] != plan['spreadsheet_id']:
        raise RuntimeError('Sumber spreadsheet berbeda.')
    live = read_live(client)
    if live['detail'] != plan['before'] or live['guard'] != plan['option_guard']:
        raise RuntimeError('Konfigurasi host berubah sejak pratinjau.')
    response, form, payload, ids = list_form(client)
    if tree_audit(client, ids) != plan['visible_trees_checked']:
        raise RuntimeError('Tree berubah sejak pratinjau.')
    source_check(api)
    target = urljoin(response.url, form.get('action') or 'host.php')
    client.require_local_url(target)
    payload.update(action='actions', drp_action='1', chk_8471='on')
    state = {'plan': plan, 'phase': 'DELETE_SELECTION_ATTEMPTED', 'attempted_at': datetime.now(timezone.utc).isoformat()}
    durable(journal, state)
    result = client.session.post(target, data=payload, timeout=30)
    soup = checked_html(result)
    # Preparation requests the legacy confirmation page only. Never submit a
    # confirmation until its host IDs and graph-preservation choice are checked.
    state['phase'] = 'CONFIRMATION_PREPARED'
    durable(journal, state)
    return result, soup, state


def confirmation_payload(soup, plan):
    forms = soup.select('form[action]')
    if len(forms) != 1:
        raise RuntimeError('Form konfirmasi ganda/tidak dikenali.')
    # Exact PHP serialization of ONE string ID; no substring/regex acceptance.
    expected = {'action': 'actions', 'drp_action': '1', 'selected_items': 'a:1:{i:0;s:4:"8471";}'}
    for name, value in expected.items():
        fields = soup.select(f'input[name="{name}"]')
        if len(fields) != 1 or fields[0].get('value') != value:
            raise RuntimeError('Konfirmasi bukan penghapusan tepat satu host 8471.')
    items = [x.get_text(' ', strip=True) for x in soup.select('li')]
    if items != [plan['before']['description']]:
        raise RuntimeError('Nama/daftar host pada konfirmasi berbeda.')
    radios = soup.select('input[name="delete_type"]')
    if len(radios) != 2 or {r.get('value') for r in radios} != {'1', '2'}:
        raise RuntimeError('Pilihan pelestarian graph/data source tidak dikenali.')
    preserve = next(r for r in radios if r.get('value') == '1')
    words = []
    for node in preserve.next_elements:
        if isinstance(node, Tag) and node.name == 'input':
            break
        if isinstance(node, NavigableString):
            words.append(str(node))
    label = ' '.join(' '.join(words).split())
    if 'Leave all Graph(s) and Data Source(s) untouched.' not in label:
        raise RuntimeError('Opsi 1 bukan preserve graphs/data sources; jangan hapus.')
    payload = form_payload(soup)
    payload.update(delete_type='1')
    return forms[0]['action'], payload


def verify_deleted(client, state, journal):
    inventory = fetch_inventory(client)
    ids = {d['host_id'] for d in inventory}
    if TARGET['host_id'] in ids:
        raise RuntimeError('Host masih ditemukan. Delete tidak diulang otomatis.')
    state['phase'] = 'HOST_ABSENT'
    durable(journal, state)
    if ids != set(state['before_host_ids']) - {TARGET['host_id']}:
        raise RuntimeError('Host target sudah hilang tetapi inventaris lain berubah; audit manual, jangan ulang delete.')
    headers = client.headers()
    if headers != state['before_headers']:
        raise RuntimeError('Host sudah hilang tetapi daftar header berubah; perlu audit manual.')
    response = client.session.get(client.base + '/tree.php', params={'action': 'edit', 'id': 34}, timeout=30)
    checked_html(response)
    if parse_tree_memberships(response.text, 34).get(8471):
        raise RuntimeError('Masih ada referensi tree ke host yang dihapus; tinjau manual.')
    state.update(phase='VERIFIED', verified_at=datetime.now(timezone.utc).isoformat())
    durable(journal, state)
    print('VERIFIED: SBS host 8471 dihapus; semua host lain dan header tetap. Mapping/riwayat HD Work dipertahankan untuk audit.')


def execute(client, api, plan, journal):
    result, soup, state = prepare_confirmation(client, api, plan, journal)
    action, payload = confirmation_payload(soup, plan)
    target = urljoin(result.url, action)
    client.require_local_url(target)
    inventory = fetch_inventory(client)
    ids = sorted(d['host_id'] for d in inventory)
    if TARGET['host_id'] not in ids:
        raise RuntimeError('Host sudah hilang sebelum konfirmasi; jangan kirim delete lagi.')
    state['before_host_ids'] = ids
    state['before_headers'] = client.headers()
    source_check(api)
    live = read_live(client)
    if live['detail'] != plan['before'] or live['guard'] != plan['option_guard']:
        raise RuntimeError('Host berubah sesaat sebelum delete; dibatalkan.')
    state.update(phase='DELETE_CONFIRM_ATTEMPTED', confirmed_at=datetime.now(timezone.utc).isoformat())
    durable(journal, state)
    client.session.post(target, data=payload, timeout=30, allow_redirects=False).raise_for_status()
    verify_deleted(client, state, journal)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--prepare', action='store_true')
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--resume', action='store_true')
    parser.add_argument('--confirm-not', action='store_true', help='Human confirmation of NOT for this exact SID; absence alone is insufficient')
    parser.add_argument('--report', default=str(PLAN))
    parser.add_argument('--journal', default=str(JOURNAL))
    parser.add_argument('--env-file', default=str(BASE / '.env'))
    args = parser.parse_args()
    if not args.confirm_not:
        parser.error('Uji manual wajib --confirm-not sesuai konfirmasi manusia atas SID 1165651712.')
    load_dotenv(args.env_file)
    api, client = None, None
    try:
        with single_instance():
            api = HDWork()
            source = source_check(api)
            client = Cacti(TARGET['server'])
            client.login()
            journal = Path(args.journal)
            if args.resume:
                state = json.loads(journal.read_text())
                validate_plan(state['plan'])
                if state.get('phase') not in ('DELETE_CONFIRM_ATTEMPTED', 'HOST_ABSENT', 'VERIFIED'):
                    raise RuntimeError('Konfirmasi delete belum dicoba; --resume tidak boleh mengirim POST.')
                verify_deleted(client, state, journal)
            elif args.apply:
                plan = json.loads(Path(args.report).read_text())
                if journal.exists():
                    previous = json.loads(journal.read_text())
                    if previous.get('phase') != 'CONFIRMATION_PREPARED' or previous.get('plan') != plan:
                        raise RuntimeError('Konfirmasi pernah dicoba/jurnal berbeda; gunakan --resume, jangan ulang delete.')
                execute(client, api, plan, journal)
            elif journal.exists():
                raise RuntimeError('Jurnal delete sudah ada; jangan ulang penghapusan.')
            elif args.prepare:
                plan = json.loads(Path(args.report).read_text())
                result, soup, _ = prepare_confirmation(client, api, plan, journal)
                # Print only non-secret confirmation structure for the pilot review.
                print(json.dumps({'forms': [{'action': f.get('action'), 'method': f.get('method')} for f in soup.select('form')],
                    'inputs': [{'name': x.get('name'), 'type': x.get('type'), 'value': x.get('value') if x.get('name') in ('action', 'selected_items', 'drp_action', 'delete_type') else '[REDACTED]'} for x in soup.select('input[name]')],
                    'selects': [{'name': x.get('name'), 'options': [{'value': o.get('value'), 'label': o.get_text(' ', strip=True)} for o in x.select('option')]} for x in soup.select('select[name]')],
                    'text': soup.get_text(' ', strip=True)[-2000:]}, ensure_ascii=False))
                print('Halaman konfirmasi diperiksa; belum mengirim konfirmasi delete.')
            else:
                plan = make_plan(client, source)
                source_check(api)
                durable(args.report, plan)
                print(json.dumps(plan, ensure_ascii=False))
                print('Pratinjau saja; tidak menghapus device/data/header.')
        return 0
    except Exception as error:
        print(str(error) if isinstance(error, RuntimeError) else type(error).__name__, file=sys.stderr)
        print('Berhenti; jangan ulang delete atau hapus jurnal bila hasil belum pasti.', file=sys.stderr)
        return 1
    finally:
        if client:
            client.close()
        if api:
            api.session.close()


if __name__ == '__main__':
    sys.exit(main())
