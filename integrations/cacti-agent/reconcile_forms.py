"""Generic guarded Cacti form I/O; no scheduling, backend or hardcoded pilot."""
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag
from bulk_sync import digest
from compare_devices import FIELDS, has_datin_identity, parse_device_detail, parse_tree_memberships
from sync_headers import checked_html, form_payload, normalize


def values(detail):
    result = {'description': detail['description'], **{key: detail[field] for key, field in FIELDS.items()}}
    for key in ('slot', 'port', 'onu'):
        if not result[key].isdigit():
            raise RuntimeError('Nilai Cacti slot/port/ONU bukan bilangan non-negatif.')
        result[key] = str(int(result[key]))
    return result


def option_guard(payload):
    excluded = {'__csrf_magic', 'description', *FIELDS.values(), 'notes', 'monitor_text'}
    return digest({key: value for key, value in payload.items() if key not in excluded})


def read_host(client, sid, host_id):
    response = client.session.get(client.base + '/host.php', params={'action': 'edit', 'id': host_id}, timeout=30)
    soup = checked_html(response)
    forms = soup.select('form[action]')
    if len(forms) != 1:
        raise RuntimeError('Form host ganda/tidak dikenali.')
    for name, value in {'id': str(host_id), 'action': 'save', 'save_component_host': '1'}.items():
        controls = soup.select(f'input[name="{name}"]')
        if len(controls) != 1 or controls[0].get('value') != value:
            raise RuntimeError('Identitas form host berubah.')
    for name in ('description', *FIELDS.values()):
        if len(soup.select(f'input[name="{name}"]')) != 1:
            raise RuntimeError('Field host hilang/ganda.')
    detail = parse_device_detail(response.text, host_id)
    if not has_datin_identity(detail['description'], sid):
        raise RuntimeError('Identitas [HD-D]/SID tidak sesuai mapping; jangan adopsi.')
    payload = form_payload(soup)
    for textarea in soup.select('textarea[name]'):
        payload[textarea['name']] = textarea.get_text()
    for control in soup.select('input[name]'):
        if control.get('type', '').lower() in ('radio', 'checkbox') and control.has_attr('checked'):
            payload[control['name']] = control.get('value', 'on')
    for name in ('monitor', 'disabled'):
        controls = soup.select(f'input[name="{name}"]')
        if len(controls) != 1 or controls[0].get('type', '').lower() != 'checkbox':
            raise RuntimeError('Checkbox monitoring tidak dikenali.')
    if 'monitor' not in payload or 'disabled' in payload:
        raise RuntimeError('Monitoring host berubah/dinonaktifkan; perlu review.')
    return {'config': values(detail), 'option_guard': option_guard(payload), 'soup': soup,
            'payload': payload, 'url': urljoin(response.url, forms[0]['action'])}


def edit_payload(live, desired):
    payload = dict(live['payload'])
    for name, value in {'description': desired['description'], **{field: desired[key] for key, field in FIELDS.items()}}.items():
        control = live['soup'].select_one(f'input[name="{name}"]')
        limit = control.get('maxlength')
        if limit and (not limit.isdigit() or len(value) > int(limit)):
            raise RuntimeError('Nilai edit melampaui batas form; tidak dipotong otomatis.')
        payload[name] = value
    return payload


def device_actions(client, sid, host_id):
    response = client.session.get(client.base + '/host.php', params={'host_template_id': -1, 'host_status': -1, 'filter': sid, 'host_rows': 5000, 'page': 1}, timeout=30)
    soup = checked_html(response)
    forms = [f for f in soup.select('form') if f.select_one('select[name="drp_action"]')]
    if len(forms) != 1:
        raise RuntimeError('Form tindakan Devices tidak dikenali.')
    form = forms[0]
    option = form.select_one('select[name="drp_action"] option[value="1"]')
    if not option or option.get_text(strip=True) != 'Delete' or len(form.select(f'input[name="chk_{host_id}"]')) != 1:
        raise RuntimeError('Host/opsi delete tidak ditemukan tepat.')
    payload = form_payload(form)
    if payload.get('action') != 'actions' or 'selected_items' in payload:
        raise RuntimeError('Form pemilihan device tidak valid.')
    ids = {int(o['value'][3:]) for o in form.select('select[name="drp_action"] option[value^="tr_"]') if o['value'][3:].isdigit()}
    return urljoin(response.url, form.get('action') or 'host.php'), payload, ids


def references(client, host_id, expected_ids=None):
    response = client.session.get(client.base + '/tree.php', timeout=30)
    soup = checked_html(response)
    ids = set()
    for link in soup.select('a[href]'):
        url = urlparse(link['href'])
        q = parse_qs(url.query)
        if url.path.split('/')[-1] == 'tree.php' and q.get('action') == ['edit'] and q.get('id', [''])[0].isdigit():
            ids.add(int(q['id'][0]))
    if not ids or client.tree not in ids or (expected_ids is not None and ids != expected_ids):
        raise RuntimeError('Inventaris tree kosong/tidak lengkap.')
    found = []
    for tree in sorted(ids):
        response = client.session.get(client.base + '/tree.php', params={'action': 'edit', 'id': tree}, timeout=30)
        checked_html(response)
        found.extend(parse_tree_memberships(response.text, tree).get(host_id, []))
    return found


def require_exclusive(refs, tree_id, header_id, absent=False):
    if absent:
        if refs:
            raise RuntimeError('Host hilang tetapi referensi tree masih ada.')
    elif len(refs) != 1 or refs[0]['tree_id'] != tree_id or refs[0]['header_id'] != header_id:
        raise RuntimeError('Host dipakai tree lain/header berubah; perlu review, jangan edit/hapus.')


def delete_confirmation(response, host_id, description):
    soup = checked_html(response)
    forms = soup.select('form[action]')
    if len(forms) != 1:
        raise RuntimeError('Form konfirmasi delete ganda/tidak dikenali.')
    allowed_serialized = {f'a:1:{{i:0;s:{len(str(host_id))}:"{host_id}";}}', f'a:1:{{i:0;i:{host_id};}}'}
    for name, accepted in {'action': {'actions'}, 'drp_action': {'1'}, 'selected_items': allowed_serialized}.items():
        controls = soup.select(f'input[name="{name}"]')
        if len(controls) != 1 or controls[0].get('value') not in accepted:
            raise RuntimeError('Konfirmasi delete bukan tepat satu host yang dikelola.')
    if [li.get_text(' ', strip=True) for li in soup.select('li')] != [description]:
        raise RuntimeError('Daftar/nama host konfirmasi berbeda.')
    radios = soup.select('input[name="delete_type"]')
    if len(radios) != 2 or {r.get('value') for r in radios} != {'1', '2'}:
        raise RuntimeError('Pilihan pelestarian graph/data source tidak dikenali.')
    words = []
    for node in next(r for r in radios if r['value'] == '1').next_elements:
        if isinstance(node, Tag) and node.name == 'input':
            break
        if isinstance(node, NavigableString):
            words.append(str(node))
    if 'Leave all Graph(s) and Data Source(s) untouched.' not in ' '.join(' '.join(words).split()):
        raise RuntimeError('Opsi preserve graph/data source berubah.')
    payload = form_payload(soup)
    payload['delete_type'] = '1'
    return urljoin(response.url, forms[0]['action']), payload
