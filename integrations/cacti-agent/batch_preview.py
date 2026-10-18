"""Read-only preview of exactly ten new DATIN devices needing new headers."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from dotenv import load_dotenv

from compare_devices import compare, fetch_inventory
from device_plan import build_device_plan
from pilot_preview import creation_profile
from sync_headers import Cacti, HDWork, SERVERS, checked_html


BATCH_LIMIT = 10
PER_SERVER = 5


def select_batch(rows):
    """Prefer repeated institutions to exercise one-header/many-hosts behavior.

    Only a balanced 5+5 CREATE batch is allowed. No mapping adoption or fallback
    to existing headers, UPDATE/HOLD rows, or smaller/larger batches.
    """
    selected, seen = [], set()
    for server in SERVERS:
        groups = defaultdict(list)
        for row in rows:
            if (row.get('server') != server or row.get('tree_id') != SERVERS[server]['tree_id']
                    or set(row.get('actions', [])) != {'CREATE_HEADER', 'CREATE_DEVICE', 'ATTACH_TO_TREE'}
                    or row.get('header_id') is not None or row.get('host_id') is not None
                    or not row.get('sid') or not row.get('institution')
                    or row.get('reasons') or row.get('institution') == 'ATCS'):
                continue
            groups[row['institution']].append(row)
        # Prefer institutions with multiple eligible SIDs, then stable names/SIDs.
        ordered = sorted(groups, key=lambda name: (-len(groups[name]), name))
        candidates = [sorted(groups[name], key=lambda row: (bool(row.get('external_devices')), row['sid'])) for name in ordered]
        chosen = []
        # First take at most 3 per header, so 5 devices normally cover 2+ headers.
        for per_header_limit in (3, PER_SERVER):
            for group in candidates:
                count = sum(row['institution'] == group[0]['institution'] for row in chosen)
                for row in group:
                    if len(chosen) == PER_SERVER or count >= per_header_limit:
                        break
                    if row['sid'] not in seen:
                        chosen.append(row)
                        seen.add(row['sid'])
                        count += 1
            if len(chosen) == PER_SERVER:
                break
        if len(chosen) != PER_SERVER:
            raise RuntimeError(f'{server}: perlu 5 SID unik valid yang membutuhkan header baru; tidak mengganti dengan tindakan lain.')
        selected.extend(chosen)
    if len(selected) != BATCH_LIMIT:
        raise RuntimeError('Jumlah batch bukan tepat 10.')
    return selected


def header_dependencies(rows):
    groups = {}
    for row in rows:
        key = (row['server'], row['institution'])
        if key not in groups:
            groups[key] = {'server': row['server'], 'tree_id': row['tree_id'], 'institution': row['institution'], 'device_sids': []}
        groups[key]['device_sids'].append(row['sid'])
    return list(groups.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', default=str(Path(__file__).with_name('.env')))
    parser.add_argument('--report', default=str(Path(__file__).with_name('batch-10.report.json')))
    args = parser.parse_args()
    load_dotenv(args.env_file)
    clients, api = [], None
    try:
        api = HDWork()
        source = api.request('GET', '/api/datin-cacti/device-source')
        mapping_source = api.request('GET', '/api/datin-cacti/device-mappings')
        if (source['spreadsheet_id'], source['snapshot_at']) != (mapping_source['spreadsheet_id'], mapping_source['snapshot_at']):
            raise RuntimeError('Snapshot sumber/mapping berbeda; ulangi pratinjau.')
        inventory, headers, forms = [], {}, {}
        for server in SERVERS:
            client = Cacti(server)
            clients.append(client)
            client.login()
            inventory.extend(fetch_inventory(client))
            headers[server] = client.headers()
            response = client.session.get(client.base + '/host.php', params={'action': 'edit', 'host_template_id': -1, 'host_status': -1}, timeout=30)
            checked_html(response)
            forms[server] = response.text
        aliases = {item['alias']: item['name'] for item in source.get('aliases', [])}
        comparison = compare(source['customers'], inventory, aliases, source_conflicts=source.get('conflicts'))
        plan = build_device_plan(comparison, mapping_source['mappings'], headers, inventory, SERVERS, aliases)
        eligible, excluded = [], []
        for row in plan['rows']:
            if 'CREATE_HEADER' not in row['actions'] or 'CREATE_DEVICE' not in row['actions']:
                continue
            try:
                row['creation_profile'] = creation_profile(forms[row['server']], row)
            except RuntimeError as error:
                excluded.append({'sid': row['sid'], 'server': row['server'], 'reason': str(error)})
                continue
            eligible.append(row)
        rows = select_batch(eligible)
        dependencies = header_dependencies(rows)
        # Check all source/mapping and header evidence again before publishing.
        current = api.request('GET', '/api/datin-cacti/device-source')
        mappings_now = api.request('GET', '/api/datin-cacti/device-mappings')
        if current['spreadsheet_id'] != source['spreadsheet_id'] or current['snapshot_at'] != source['snapshot_at'] or mappings_now != mapping_source:
            raise RuntimeError('Sumber/mapping berubah saat scan; ulangi, bukan menyetujui laporan lama.')
        for client in clients:
            live_headers = client.headers()
            if live_headers != headers[client.server]:
                raise RuntimeError('Header berubah selama scan; ulangi pratinjau.')
        report = {'mode': 'BATCH_PREVIEW_ONLY', 'approved': False, 'executable': False,
                  'time': datetime.now(timezone.utc).isoformat(), 'spreadsheet_id': source['spreadsheet_id'],
                  'snapshot_at': source['snapshot_at'], 'limit': BATCH_LIMIT, 'devices': rows,
                  'headers': dependencies, 'excluded_invalid_forms': excluded,
                  'summary': {'CREATE_DEVICE': len(rows), 'ATTACH_TO_TREE': len(rows), 'CREATE_HEADER_UNIQUE': len(dependencies),
                              'by_server': dict(Counter(row['server'] for row in rows))}}
        Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
        for header in dependencies:
            print(f"HEADER BARU {header['server']}: {header['institution']} ({len(header['device_sids'])} device)")
        for row in rows:
            print(f"{row['server']} | {row['sid']} | {row['description']}")
        print(json.dumps(report['summary'], ensure_ascii=False))
        print(f'Pratinjau saja: {args.report}. Tidak menulis header/device/reservasi/mapping.')
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
