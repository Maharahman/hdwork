"""Pure, read-only action planner. No Cacti writes or mapping adoption."""
from collections import Counter


def build_device_plan(report, mappings, headers, inventory, servers, aliases=None, protected=None):
    aliases = aliases or {}
    protected = protected or {"ATCS"}
    def canonical(value):
        name = " ".join(str(value or "").split()).upper()
        return aliases.get(name, name)

    rows = []
    for result in report["results"]:
        server = result.get("expected_server")
        institution = canonical(result.get("institution"))
        config = servers.get(server, {})
        wanted_headers = [h for name, entries in headers.get(server, {}).items()
                          if canonical(name) == institution for h in entries]
        stored = [m for m in mappings if m["sid"] == result["sid"]]
        candidates = [d for d in result["candidates"] if d.get("candidate_role") == "PREFIX_CANDIDATE"]
        external = [d for d in result["candidates"] if d.get("candidate_role") == "EXTERNAL"]
        plan = {
            "sid": result["sid"], "source_row": result.get("source_row"), "server": server,
            "tree_id": config.get("tree_id"), "institution": institution,
            "header_id": wanted_headers[0]["header_id"] if len(wanted_headers) == 1 else None,
            "description": result.get("planned_description"), "desired": result.get("spreadsheet", {}),
            "external_devices": [{"server": d["server"], "host_id": d["host_id"]} for d in external],
            "actions": [], "reasons": [], "approved": False,
        }
        reasons = plan["reasons"]
        if report.get("source_changed_during_scan"):
            reasons.append("Snapshot berubah selama scan")
        if result["status"] in {"SOURCE_CONFLICT", "INCOMPLETE", "AMBIGUOUS", "PROTECTED", "READ_ERROR", "OTHER_SERVER"}:
            reasons.extend(result.get("reasons") or [result["status"]])
        if result["status"] not in {"NOT_FOUND", "EXTERNAL_ONLY", "MATCH", "LOCATION_DIFF", "HEADER_DIFF", "SOURCE_CONFLICT", "INCOMPLETE", "AMBIGUOUS", "PROTECTED", "READ_ERROR", "OTHER_SERVER"}:
            reasons.append("Status perbandingan tidak dikenali")
        if institution in {canonical(name) for name in protected}:
            reasons.append("Instansi monitoring khusus dilindungi")
        if len(wanted_headers) > 1:
            reasons.append("Header instansi ganda")
        if len(stored) > 1:
            reasons.append("Mapping SID ganda")
        if stored:
            mapping = stored[0]
            plan["host_id"] = mapping["host_id"]
            if mapping.get("ownership_status") != "VERIFIED" or not mapping.get("verified_at"):
                reasons.append("Mapping belum diverifikasi")
            if mapping["server"] != server or mapping["tree_id"] != config.get("tree_id"):
                reasons.append("Server/tree mapping berbeda; perpindahan harus ditinjau")
            live = [d for d in inventory if d["server"] == mapping["server"] and d["host_id"] == mapping["host_id"]]
            if len(live) != 1 or not any(d["server"] == mapping["server"] and d["host_id"] == mapping["host_id"] for d in candidates):
                reasons.append("Host mapping hilang atau identitas prefix/SID berubah; jangan buat pengganti otomatis")
        elif candidates:
            reasons.append("Kandidat bertanda belum memiliki mapping; perlu konfirmasi, bukan CREATE")
        if reasons:
            plan["actions"] = ["HOLD"]
        else:
            if not wanted_headers:
                plan["actions"].append("CREATE_HEADER")
            if not stored:
                plan["actions"].extend(["CREATE_DEVICE", "ATTACH_TO_TREE"])
            else:
                candidate = next(d for d in candidates if d["host_id"] == stored[0]["host_id"] and d["server"] == server)
                plan["before"] = candidate.get("detail")
                if result["differences"] or (candidate.get("detail") or {}).get("description") != plan["description"]:
                    plan["actions"].append("UPDATE_DEVICE")
                memberships = candidate["memberships"]
                # Do not plan destructive tree movement or detach shared hosts.
                if memberships and (len(memberships) != 1 or memberships[0].get("header_id") != plan["header_id"]):
                    plan["actions"] = ["HOLD"]
                    reasons.append("Keanggotaan header berbeda/ganda; perpindahan harus ditinjau")
                elif not memberships:
                    plan["actions"].append("ATTACH_TO_TREE")
                if not plan["actions"]:
                    plan["actions"] = ["NO_CHANGE"]
        rows.append(plan)
    return {"mode": "PREVIEW_ONLY", "executable": False, "mapping_writes": False,
            "rows": rows, "summary": dict(Counter(a for row in rows for a in row["actions"]))}
