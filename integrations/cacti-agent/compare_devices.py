"""Read-only DATIN spreadsheet/Cacti comparison. No --apply or adoption option."""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from dotenv import load_dotenv

from sync_headers import Cacti, HDWork, SERVERS, checked_html, normalize


FIELDS = {"olt_ip": "hostname", "slot": "slot", "port": "port", "onu": "onu_id", "serial_number": "onu_sn"}


def contains_sid(description, sid):
    # Match the full identifier: 123 must not match 0123, 1234, ABC123, or 470-123.
    return bool(sid and re.search(r"(?<![A-Za-z0-9-])" + re.escape(str(sid)) + r"(?![A-Za-z0-9-])", description, re.IGNORECASE))


def canonical_name(value, aliases):
    name = normalize(value)
    return aliases.get(name, name)


def canonical_number(value):
    text = str(value or "").strip()
    return str(int(text)) if re.fullmatch(r"\d+", text) else text


def validate_customer(customer):
    reasons = []
    for field in ("sid", "customer_name", "olt_ip", "slot", "port", "onu", "serial_number"):
        if not str(customer.get(field) or "").strip():
            reasons.append(f"Kolom {field} kosong")
    if normalize(customer.get("status", "")) != "OK":
        reasons.append("STATUS bukan OK")
    if normalize(customer.get("server", "")) not in SERVERS:
        reasons.append("SERVER bukan SBS/SBU")
    if customer.get("olt_ip"):
        try:
            ipaddress.ip_address(customer["olt_ip"].strip())
        except ValueError:
            reasons.append("IP OLT tidak valid")
    for field in ("slot", "port", "onu"):
        text = str(customer.get(field) or "").strip()
        if text and (not re.fullmatch(r"\d+", text) or int(text) < 1):
            reasons.append(f"{field} harus bilangan bulat positif")
    return reasons


def parse_device_list(html):
    soup = BeautifulSoup(html, "html.parser")
    count = re.search(r"Showing\s+Rows\s+\d+\s+to\s+\d+\s+of\s+([\d,]+)", soup.get_text(" ", strip=True), re.IGNORECASE)
    if not count:
        raise RuntimeError("Jumlah total Devices tidak terbaca; inventaris dianggap belum lengkap.")
    devices = {}
    for link in soup.select('a[href]'):
        url = urlparse(link["href"])
        query = parse_qs(url.query)
        if url.path.split("/")[-1] == "host.php" and query.get("action") == ["edit"] and query.get("id", [""])[0].isdigit():
            description = link.get_text(" ", strip=True)
            if not description:
                continue
            host_id = int(query["id"][0])
            devices[host_id] = {"host_id": host_id, "description": description, "memberships": []}
    total = int(count.group(1).replace(",", ""))
    if len(devices) != total:
        raise RuntimeError(f"Daftar Devices belum lengkap: terbaca {len(devices)} dari {total}. Tidak membuat kesimpulan SID belum ditemukan.")
    return list(devices.values())


def parse_tree_memberships(html, tree_id):
    soup = BeautifulSoup(html, "html.parser")
    header = None
    memberships = defaultdict(list)
    for row in soup.select("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        kind = cells[1].get_text(strip=True)
        if kind == "Heading":
            header = None
            for link in cells[0].select("a[href]"):
                query = parse_qs(urlparse(link["href"]).query)
                if query.get("action") == ["item_edit"] and query.get("tree_id") == [str(tree_id)] and query.get("id"):
                    header = {"header_id": int(query["id"][0]), "header": link.get_text(" ", strip=True), "tree_id": tree_id}
                    break
        elif kind == "Host":
            for link in cells[0].select("a[href]"):
                query = parse_qs(urlparse(link["href"]).query)
                if urlparse(link["href"]).path.split("/")[-1] == "host.php" and query.get("action") == ["edit"] and query.get("id"):
                    memberships[int(query["id"][0])].append(header or {"tree_id": tree_id, "header": "[root]", "header_id": 0})
    return memberships


def parse_device_detail(html, host_id):
    soup = BeautifulSoup(html, "html.parser")
    values = {}
    for field in ("description", *FIELDS.values()):
        element = soup.find("input", attrs={"name": field}) or soup.find("input", id=field)
        if element is None:
            raise RuntimeError(f"Field {field} tidak ditemukan pada host {host_id}; form tidak dapat dibandingkan.")
        values[field] = element.get("value", "").strip()
    identity = soup.find("input", attrs={"name": "id"})
    if identity and str(identity.get("value")) != str(host_id):
        raise RuntimeError("Host ID hasil baca tidak sesuai request.")
    return values


def is_protected(device, protected):
    return bool(re.match(r"^ATCS(?:[_\s-]|$)", normalize(device["description"]))) or any(
        normalize(item["header"]) in protected for item in device["memberships"]
    )


def compare(customers, inventory, aliases=None, protected=None, source_conflicts=None):
    aliases = aliases or {}
    protected = protected or {"ATCS"}
    source_conflicts = source_conflicts or []
    counts = Counter(customer["sid"].strip().upper() for customer in customers)
    results = []
    referenced = set()
    for customer in customers:
        sid = customer["sid"].strip()
        server = normalize(customer.get("server", ""))
        matches = [device for device in inventory if contains_sid(device["description"], sid)]
        local = [device for device in matches if device["server"] == server]
        elsewhere = [device for device in matches if device["server"] != server]
        for device in matches:
            referenced.add((device["server"], device["host_id"]))
        issues = validate_customer(customer)
        status = "MATCH"
        differences = []
        detail = local[0].get("detail") if len(local) == 1 else None
        if counts[sid.upper()] > 1 or len(local) > 1:
            status = "AMBIGUOUS"
            issues.append("SID sumber atau device pada server tujuan ditemukan lebih dari satu")
        elif any(is_protected(device, protected) for device in matches):
            status = "PROTECTED"
            issues.append("SID bersinggungan dengan perangkat/header monitoring khusus")
        elif issues:
            status = "INCOMPLETE"
        elif local and elsewhere:
            status = "AMBIGUOUS"
            issues.append("SID ditemukan di kedua server; perlu pemeriksaan perpindahan/duplikasi")
        elif not local and elsewhere:
            status = "OTHER_SERVER"
            issues.append("SID hanya ditemukan pada server berbeda")
        elif not local:
            status = "NOT_FOUND"
            issues.append("SID tidak ditemukan pada nama seluruh device; bukan izin membuat device baru")
        elif local[0].get("detail_error") or detail is None:
            status = "READ_ERROR"
            issues.append(local[0].get("detail_error") or "Form detail device belum terbaca")
        else:
            for source_field, cacti_field in FIELDS.items():
                expected = str(customer.get(source_field) or "").strip()
                actual = detail[cacti_field]
                if source_field in ("slot", "port", "onu"):
                    expected, actual = canonical_number(expected), canonical_number(actual)
                elif source_field == "serial_number":
                    expected, actual = normalize(expected), normalize(actual)
                if expected != actual:
                    differences.append({"field": source_field, "spreadsheet": expected, "cacti": actual})
            expected_header = canonical_name(customer["customer_name"], aliases)
            memberships = local[0]["memberships"]
            if len(memberships) != 1 or canonical_name(memberships[0]["header"], aliases) != expected_header:
                issues.append("Header DATIN belum sesuai atau host muncul beberapa kali pada tree DATIN")
            if differences:
                status = "LOCATION_DIFF"
                issues.append("IP OLT/slot/port/ONU/SN berbeda")
            elif issues:
                status = "HEADER_DIFF"
            if not contains_sid(detail["description"], sid):
                status = "READ_ERROR"
                issues.append("Identitas device berubah selama pembacaan")
        results.append({
            "sid": sid, "source_row": customer.get("source_row"), "institution": customer.get("customer_name", ""),
            "customer_detail": customer.get("customer_detail", ""),
            "expected_server": server, "status": status, "reasons": issues, "differences": differences,
            "spreadsheet": {key: customer.get(key, "") for key in FIELDS},
            "candidates": [{"server": d["server"], "host_id": d["host_id"], "description": d["description"], "memberships": d["memberships"], "detail": d.get("detail"), "detail_error": d.get("detail_error")} for d in matches],
            "ownership": "UNREVIEWED",
        })
    for conflict in source_conflicts:
        results.append({"sid": conflict["sid"], "status": "SOURCE_CONFLICT", "reasons": ["SID konflik pada spreadsheet; seluruh baris ditahan"], "source_rows": conflict["rows"], "candidates": [], "differences": [], "ownership": "UNREVIEWED"})
        for device in inventory:
            if contains_sid(device["description"], conflict["sid"]):
                referenced.add((device["server"], device["host_id"]))
    unmanaged = [{"server": d["server"], "host_id": d["host_id"], "description": d["description"], "memberships": d["memberships"], "status": "PROTECTED" if is_protected(d, protected) else "UNMAPPED"} for d in inventory if (d["server"], d["host_id"]) not in referenced]
    return {"results": results, "unmanaged": unmanaged, "summary": dict(Counter(row["status"] for row in results)), "ownership_changed": False}


def fetch_inventory(client):
    # The live UI exposes host_rows up to 5000. Abort if not all rows fit/read.
    response = client.session.get(client.base + "/host.php", params={"host_template_id": -1, "host_status": -1, "filter": "", "host_rows": 5000, "page": 1}, timeout=60)
    soup = checked_html(response)
    for name in ("host_template_id", "host_status"):
        element = soup.select_one(f'select[name="{name}"]')
        selected = element.select_one("option[selected]") if element else None
        if selected is None or selected.get("value") != "-1":
            raise RuntimeError("Filter Devices belum Any; inventaris tidak lengkap.")
    filter_input = soup.select_one('input[name="filter"]')
    if filter_input and filter_input.get("value", "").strip():
        raise RuntimeError("Filter pencarian Devices belum kosong.")
    devices = parse_device_list(response.text)
    tree = client.session.get(client.base + "/tree.php", params={"action": "edit", "id": client.tree}, timeout=30)
    checked_html(tree)
    memberships = parse_tree_memberships(tree.text, client.tree)
    for device in devices:
        device.update(server=client.server, memberships=memberships.get(device["host_id"], []))
    return devices


def safe_csv(value):
    text = str(value or "")
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def write_reports(prefix, report):
    prefix = Path(prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    Path(str(prefix) + ".json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    columns = ["status", "sid", "source_row", "institution", "customer_detail", "expected_server", "cacti_server", "host_id", "cacti_description", "cacti_header", "reasons"]
    for field in FIELDS:
        columns.extend(["sheet_" + field, "cacti_" + field])
    with Path(str(prefix) + ".csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for result in report["results"]:
            for candidate in result["candidates"] or [{}]:
                row = {key: result.get(key, "") for key in columns}
                row.update(cacti_server=candidate.get("server", ""), host_id=candidate.get("host_id", ""), cacti_description=candidate.get("description", ""), cacti_header="; ".join(item["header"] for item in candidate.get("memberships", [])), reasons="; ".join(result["reasons"]))
                for source_field, cacti_field in FIELDS.items():
                    row["sheet_" + source_field] = result.get("spreadsheet", {}).get(source_field, "")
                    row["cacti_" + source_field] = (candidate.get("detail") or {}).get(cacti_field, "")
                writer.writerow({key: safe_csv(value) for key, value in row.items()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=str(Path(__file__).with_name(".env")))
    parser.add_argument("--report-prefix", default=str(Path(__file__).with_name("comparison.report")))
    parser.add_argument("--protected-header", action="append", default=["ATCS"])
    args = parser.parse_args()
    load_dotenv(args.env_file)
    clients = []
    hdwork = None
    try:
        hdwork = HDWork()
        source = hdwork.request("GET", "/api/datin-cacti/device-source")
        inventory = []
        for server in SERVERS:
            client = Cacti(server)
            clients.append(client)
            client.login()
            devices = fetch_inventory(client)
            print(f"{server}: {len(devices)} device ditemukan", flush=True)
            inventory.extend(devices)
        customers = source["customers"]
        # Fetch detail only for SID candidates; avoid pinging thousands of unrelated hosts.
        for index, device in enumerate(inventory):
            if not any(contains_sid(device["description"], customer["sid"]) for customer in customers):
                continue
            client = next(client for client in clients if client.server == device["server"])
            try:
                response = client.session.get(client.base + "/host.php", params={"action": "edit", "id": device["host_id"]}, timeout=30)
                checked_html(response)
                device["detail"] = parse_device_detail(response.text, device["host_id"])
            except Exception as error:
                device["detail_error"] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            print(f"{device['server']}: baca host {device['host_id']}", flush=True)
            time.sleep(0.1)
        result = compare(customers, inventory, {item["alias"]: item["name"] for item in source.get("aliases", [])}, {normalize(name) for name in args.protected_header}, source.get("conflicts"))
        # Reject reports if the source changed during a long inventory pass.
        current = hdwork.request("GET", "/api/datin-cacti/device-source")
        result.update(time=datetime.now(timezone.utc).isoformat(), spreadsheet_id=source["spreadsheet_id"], snapshot_at=source["snapshot_at"], source_changed_during_scan=current["snapshot_at"] != source["snapshot_at"], inventory_counts=dict(Counter(device["server"] for device in inventory)), source_invalid_rows=source.get("invalid_rows", []), detail="READ_ONLY; no ownership adoption; memberships cover DATIN trees only; SID search is description-based, no global SN fallback")
        write_reports(args.report_prefix, result)
        print(json.dumps(result["summary"], ensure_ascii=False))
        print(f"Laporan: {args.report_prefix}.json dan {args.report_prefix}.csv")
        if result["source_changed_during_scan"]:
            print("Snapshot berubah saat scan: laporan untuk tinjauan saja; jalankan ulang sebelum menyetujui mapping.", file=sys.stderr)
            return 2
        return 1 if result["summary"].get("READ_ERROR") else 0
    except Exception as error:
        print(str(error) if isinstance(error, RuntimeError) else type(error).__name__, file=sys.stderr)
        return 1
    finally:
        for client in clients:
            client.close()
        if hdwork:
            hdwork.session.close()


if __name__ == "__main__":
    sys.exit(main())
