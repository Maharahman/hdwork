"""Create missing DATIN institution headers. Default is read-only preview.

Run on a computer connected to the Cacti VPN. No host creation/edit/deletion.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv


SERVERS = {
    "SBS": {"base_url": "http://10.96.33.156/ont", "tree_id": 34, "tree_name": "DATIN SBS"},
    "SBU": {"base_url": "http://10.100.2.234/ont", "tree_id": 28, "tree_name": "DATIN SBU"},
}


def normalize(value):
    return " ".join(str(value).split()).upper()


def checked_html(response):
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    if soup.select_one('input[name="login_username"]'):
        raise RuntimeError("Sesi Cacti belum login atau kedaluwarsa.")
    return soup


def form_payload(form):
    """Preserve form/CSRF hidden fields instead of hardcoding Cacti internals."""
    payload = {}
    for field in form.select("input[name]"):
        if field.get("type", "text").lower() not in ("button", "submit", "checkbox", "radio", "file"):
            payload[field["name"]] = field.get("value", "")
    for field in form.select("select[name]"):
        option = field.select_one("option[selected]") or field.select_one("option")
        if option:
            payload[field["name"]] = option.get("value", "")
    return payload


def parse_headers(html, base_url, tree_id):
    soup = BeautifulSoup(html, "html.parser")
    headers = {}
    for row in soup.select("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 2 or cells[1].get_text(strip=True) != "Heading":
            continue
        for link in cells[0].select("a[href]"):
            title = link.get_text(" ", strip=True)
            url = urlparse(urljoin(base_url + "/", link["href"]))
            query = parse_qs(url.query)
            if query.get("action") != ["item_edit"] or query.get("tree_id") != [str(tree_id)] or "id" not in query or not title:
                continue
            key = normalize(title)
            entry = {"name": title, "header_id": int(query["id"][0])}
            headers.setdefault(key, []).append(entry)
    return headers


class Cacti:
    def __init__(self, server, session=None):
        self.server = server
        self.config = SERVERS[server]
        self.base = self.config["base_url"]
        self.tree = self.config["tree_id"]
        self.session = session or requests.Session()

    def login(self):
        username = os.environ.get(f"CACTI_{self.server}_USERNAME")
        password = os.environ.get(f"CACTI_{self.server}_PASSWORD")
        if not username or not password:
            raise RuntimeError(f"Isi CACTI_{self.server}_USERNAME dan CACTI_{self.server}_PASSWORD.")
        url = self.base + "/index.php"
        response = self.session.get(url, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        field = soup.select_one('input[name="login_username"]')
        if field:
            form = field.find_parent("form")
            if not form:
                raise RuntimeError("Form login Cacti tidak dikenali.")
            payload = form_payload(form)
            payload.update(action="login", login_username=username, login_password=password)
            target = urljoin(url, form.get("action") or url)
            self.require_local_url(target)
            checked_html(self.session.post(target, data=payload, timeout=20))
        self.headers()  # Verify the session can actually read the configured DATIN tree.

    def require_local_url(self, url):
        if urlparse(url).netloc != urlparse(self.base).netloc or not urlparse(url).path.startswith("/ont/"):
            raise RuntimeError("Form mengarah ke luar server Cacti yang dikonfigurasi.")

    def headers(self):
        response = self.session.get(self.base + "/tree.php", params={"action": "edit", "id": self.tree}, timeout=20)
        soup = checked_html(response)
        name = soup.select_one('input[name="name"]')
        if not name or normalize(name.get("value", "")) != self.config["tree_name"]:
            raise RuntimeError(f"Tree {self.tree} tidak cocok dengan {self.config['tree_name']} atau tidak dapat diakses.")
        return parse_headers(response.text, self.base, self.tree)

    def ensure_header(self, title, apply=False):
        key = normalize(title)
        matches = self.headers().get(key, [])
        if len(matches) > 1:
            raise RuntimeError(f"Header duplikat di Cacti: {title}. Perlu diperiksa manual.")
        if matches:
            return {"name": title, "server": self.server, "tree_id": self.tree, "header_id": matches[0]["header_id"], "action": "existing"}
        if not apply:
            return {"name": title, "server": self.server, "tree_id": self.tree, "action": "would_create"}

        response = self.session.get(self.base + "/tree.php", params={"action": "item_edit", "tree_id": self.tree, "parent_id": 0}, timeout=20)
        soup = checked_html(response)
        title_field = soup.select_one('input[name="title"]')
        form = title_field.find_parent("form") if title_field else None
        if not form:
            raise RuntimeError("Form pembuatan header tidak dikenali.")
        parent = form.select_one('select[name="parent_item_id"] option[value="0"]')
        item_type = form.select_one('select[name="type_select"]')
        selected = item_type.select_one("option[selected]") or item_type.select_one("option") if item_type else None
        if not parent or not selected or selected.get_text(strip=True) != "Header":
            raise RuntimeError("Form bukan Header pada tingkat root.")
        payload = form_payload(form)
        payload.update(title=title, parent_item_id="0", sort_children_type="1")
        target = urljoin(response.url, form.get("action") or "tree.php")
        self.require_local_url(target)
        # Never blindly retry a creation POST: the server might already have committed it.
        uncertain = False
        try:
            checked_html(self.session.post(target, data=payload, timeout=25))
        except requests.RequestException:
            uncertain = True
        matches = self.headers().get(key, [])
        if len(matches) != 1:
            raise RuntimeError(f"Create belum terverifikasi untuk {title}; tidak diulang otomatis. Jalankan ulang agar header diperiksa lagi.")
        return {"name": title, "server": self.server, "tree_id": self.tree, "header_id": matches[0]["header_id"], "action": "verified_after_timeout" if uncertain else "created"}

    def close(self):
        self.session.close()


class HDWork:
    def __init__(self):
        self.base = os.environ.get("HDWORK_BASE_URL", "https://serverhdwork.telkomakses-area3.id").rstrip("/")
        key = os.environ.get("DATIN_CACTI_AGENT_API_KEY")
        if not self.base.startswith("https://") or not key:
            raise RuntimeError("Isi HDWORK_BASE_URL HTTPS dan DATIN_CACTI_AGENT_API_KEY.")
        self.session = requests.Session()
        self.session.headers["X-Cacti-Agent-Key"] = key

    def request(self, method, path, payload=None):
        response = self.session.request(method, self.base + path, json=payload, timeout=30, allow_redirects=False)
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"HD Work HTTP {response.status_code}; periksa endpoint, key, migrasi, dan umur snapshot.")
        data = response.json()
        if not data.get("success"):
            raise RuntimeError("HD Work tidak mengonfirmasi keberhasilan request.")
        return data

    def plan(self):
        return self.request("GET", "/api/datin-cacti/header-plan")

    def save(self, plan, mappings):
        return self.request("POST", "/api/datin-cacti/header-mappings", {
            "spreadsheet_id": plan["spreadsheet_id"], "snapshot_at": plan["snapshot_at"], "mappings": mappings,
        })


@contextmanager
def single_instance():
    # Mac/Linux: prevent an overlapping cron/manual run on the same agent machine.
    import fcntl
    path = Path(__file__).with_name(".sync_headers.lock")
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Agent header lain masih berjalan pada komputer ini.") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run(args):
    hdwork = HDWork()
    clients = {}
    report = {"time": datetime.now(timezone.utc).isoformat(), "mode": "apply" if args.apply else "preview", "results": [], "errors": []}
    try:
        plan = hdwork.plan()
        report["snapshot_at"] = plan["snapshot_at"]
        report["skipped"] = plan.get("skipped", [])
        for item in plan["institutions"]:
            if args.server != "ALL" and item["server"] != args.server:
                continue
            server = item["server"]
            try:
                if server not in SERVERS or item["tree_id"] != SERVERS[server]["tree_id"]:
                    raise RuntimeError("Server/tree pada rencana tidak dikenali.")
                if server not in clients:
                    client = Cacti(server)
                    clients[server] = client
                    client.login()
                # Recheck eligibility immediately before a possible remote write.
                current = hdwork.plan() if args.apply else plan
                if not any(group["server"] == server and normalize(group["name"]) == normalize(item["name"]) for group in current["institutions"]):
                    raise RuntimeError("Instansi tidak lagi ada pada snapshot valid terbaru.")
                result = clients[server].ensure_header(item["name"], apply=args.apply)
                report["results"].append(result)
                print(f"{server} | {item['name']} | {result['action']} | ID {result.get('header_id', '-')}")
                if args.apply:
                    hdwork.save(current, [result])
            except Exception as error:
                message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                report["errors"].append({"server": server, "name": item["name"], "error": message})
                print(f"GAGAL {server} | {item['name']} | {message}", file=sys.stderr)
                # Stop on errors; rerunning discovers any headers already created.
                break
    finally:
        for client in clients.values():
            client.close()
        hdwork.session.close()
        if args.report:
            Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return 1 if report["errors"] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Buat header yang belum ada dan simpan mapping ke HD Work")
    parser.add_argument("--server", choices=["ALL", "SBS", "SBU"], default="ALL")
    parser.add_argument("--env-file", default=str(Path(__file__).with_name(".env")))
    parser.add_argument("--report", help="Simpan laporan JSON ke file lokal")
    args = parser.parse_args()
    load_dotenv(args.env_file)
    try:
        with single_instance():
            return run(args)
    except Exception as error:
        print(str(error) if isinstance(error, RuntimeError) else type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
