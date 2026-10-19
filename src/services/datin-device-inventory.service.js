const fail = (message) => Object.assign(new Error(message), { statusCode: 400 });
const norm = (s) => String(s ?? "").trim().replace(/\s+/g, " ").toUpperCase();

// Full SID/status inventory is separate from the OK-only customer payload.
// Missing / incomplete inventory can never authorize an automatic deletion.
function prepareDeviceInventory(payload) {
  const inv = payload.device_inventory;
  if (inv === undefined) return null;
  if (!inv || inv.version !== 1 || typeof inv.complete !== "boolean" || !Array.isArray(inv.rows)
      || !Number.isSafeInteger(inv.total_rows) || inv.total_rows < 0 || inv.total_rows > 10000
      || !Number.isSafeInteger(inv.blank_rows) || inv.blank_rows < 0 || inv.rows.length + inv.blank_rows !== inv.total_rows) throw fail("Inventaris SID/status lengkap tidak valid.");
  const byRow = new Map(), groups = new Map();
  for (const row of inv.rows) {
    if (!row || !Number.isSafeInteger(row.source_row) || row.source_row < 2 || row.source_row > inv.total_rows + 1 || byRow.has(row.source_row)
        || ["sid", "status", "server", "customer_name"].some(k => typeof row[k] !== "string")
        || row.sid.length > 128 || row.status.length > 32 || row.server.length > 32 || row.customer_name.length > 255) throw fail("Baris inventaris SID/status tidak valid/ganda.");
    const entry = { source_row: row.source_row, sid: row.sid.trim(), status: norm(row.status), server: norm(row.server), customer_name: norm(row.customer_name) };
    byRow.set(entry.source_row, entry);
    if (entry.sid) {
      const key = norm(entry.sid);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(entry);
    }
  }
  const ok = payload.rows.filter(r => norm(r.status) === "OK");
  if (ok.length !== inv.rows.filter(r => norm(r.status) === "OK").length) throw fail("Jumlah OK dan inventaris SID berbeda.");
  const seen = new Set();
  for (const r of ok) {
    const e = byRow.get(r.source_row);
    if (!e || seen.has(r.source_row) || e.sid !== String(r.sid ?? "").trim() || e.status !== "OK"
        || e.server !== norm(r.server) || e.customer_name !== norm(r.customer_name)) throw fail("Data OK dan inventaris SID tidak konsisten.");
    seen.add(r.source_row);
  }
  const institutions = payload.institution_inventory;
  if (institutions && (institutions.total_rows !== inv.total_rows || institutions.blank_rows !== inv.blank_rows
      || institutions.complete !== inv.complete || institutions.rows.length !== inv.rows.length
      || institutions.rows.some(r => { const e = byRow.get(r.source_row); return !e || norm(r.customer_name) !== e.customer_name || norm(r.status) !== e.status; }))) throw fail("Inventaris instansi dan inventaris SID berbeda.");
  return { version: 1, complete: inv.complete, total_rows: inv.total_rows, blank_rows: inv.blank_rows,
    rows: [...byRow.values()], conflicts: [...groups.values()].filter(rows => rows.length > 1).map(rows => ({ sid: rows[0].sid, rows: rows.map(r => r.source_row) })) };
}

async function saveDeviceInventory(connection, payload, inventory, records, snapshotAt) {
  if (!inventory) return { status: "SKIPPED", reason: "Apps Script belum mengirim inventaris SID/status" };
  if (!inventory.complete) return { status: "INCOMPLETE", reason: "Snapshot parsial tidak mengotorisasi perubahan Cacti" };
  // A snapshot losing half of the rows is still saved, but cannot delete devices.
  const [[previous]] = await connection.query("SELECT device_inventory_json FROM datin_sheet_sources WHERE spreadsheet_id = ?", [payload.spreadsheet_id]);
  const old = typeof previous?.device_inventory_json === "string" ? JSON.parse(previous.device_inventory_json) : previous?.device_inventory_json;
  let baseline = old?.guard_rows || old?.rows?.length || 0;
  if (!baseline) {
    const [[stored]] = await connection.query("SELECT COUNT(*) AS total FROM datin_customer_master WHERE spreadsheet_id = ?", [payload.spreadsheet_id]);
    baseline = Number(stored?.total || 0);
  }
  // Count real business rows, not formatted/blank grid rows that could conceal
  // an accidental wipe. On first upgrade, historical masters provide a floor.
  inventory.safe_for_delete = inventory.rows.length > 0 && !(baseline && inventory.rows.length <= baseline / 2);
  inventory.guard_rows = inventory.safe_for_delete ? inventory.rows.length : baseline;
  for (const r of records) await connection.query("UPDATE datin_customer_master SET source_status = 'OK', source_status_snapshot_at = ? WHERE spreadsheet_id = ? AND sid = ?", [snapshotAt, payload.spreadsheet_id, r.data.sid]);
  const conflicts = new Set(inventory.conflicts.map(r => norm(r.sid)));
  const not = inventory.rows.filter(r => r.sid && r.status === "NOT" && !conflicts.has(norm(r.sid)));
  for (const r of not) await connection.query("UPDATE datin_customer_master SET source_status = 'NOT', source_status_snapshot_at = ? WHERE spreadsheet_id = ? AND sid = ?", [snapshotAt, payload.spreadsheet_id, r.sid]);
  for (const r of inventory.conflicts) await connection.query("UPDATE datin_customer_master SET source_status = 'CONFLICT', source_status_snapshot_at = ? WHERE spreadsheet_id = ? AND sid = ?", [snapshotAt, payload.spreadsheet_id, r.sid]);
  await connection.query("UPDATE datin_sheet_sources SET device_inventory_json = ?, device_inventory_snapshot_at = ? WHERE spreadsheet_id = ?", [JSON.stringify(inventory), snapshotAt, payload.spreadsheet_id]);
  return { status: "COMPLETE", rows: inventory.rows.length, not: not.length, conflicts: inventory.conflicts.length, safe_for_delete: inventory.safe_for_delete };
}
module.exports = { prepareDeviceInventory, saveDeviceInventory };
