const crypto = require("crypto");
const pool = require("../config/database");
const { prepareInstitutionInventory, syncInstitutions } = require("./datin-institution-sync.service");
const { prepareDeviceInventory, saveDeviceInventory } = require("./datin-device-inventory.service");

const SOURCE_ID = "16tSELEZFzbH7FdI3NFqnNJx17xVf8GXgvZD5ghfxkLE";
const FIELDS = ["sid", "service", "customer_category", "customer_name", "customer_detail", "address", "access_node", "fsp", "serial_number", "slot", "port", "onu", "olt_ip", "server", "status"];

function invalid(message, statusCode = 400) {
  return Object.assign(new Error(message), { statusCode });
}

function prepareSnapshot(payload) {
  if (!payload || payload.spreadsheet_id !== (process.env.DATIN_SHEET_ID || SOURCE_ID) || payload.sheet_name !== "Sheet1") {
    throw invalid("Sumber spreadsheet atau tab tidak sesuai.");
  }
  if (!/^[0-9a-f-]{36}$/i.test(payload.run_id || "") || !Number.isFinite(Date.parse(payload.snapshot_at))) {
    throw invalid("run_id atau snapshot_at tidak valid.");
  }
  const snapshotAt = new Date(payload.snapshot_at);
  if (snapshotAt.getTime() > Date.now() + 5 * 60 * 1000) throw invalid("Waktu snapshot terlalu jauh di masa depan.");
  if (!Array.isArray(payload.rows) || payload.rows.length > 10000) throw invalid("Maksimal 10.000 baris per sinkronisasi.");
  const summary = { received: payload.rows.length, ignored: 0, created: 0, updated: 0, unchanged: 0, conflicts: [], invalid_rows: [] };
  const bySid = new Map();
  for (const row of payload.rows) {
    if (!row || typeof row !== "object") throw invalid("Format baris tidak valid.");
    if (String(row.status || "").trim().toUpperCase() !== "OK") { summary.ignored += 1; continue; }
    if (!Number.isSafeInteger(row.source_row) || row.source_row < 2) throw invalid("Nomor baris sumber tidak valid.");
    const data = {};
    for (const key of FIELDS) {
      if (row[key] != null && typeof row[key] !== "string") throw invalid(`Kolom ${key} harus berupa teks.`);
      data[key] = String(row[key] || "").trim();
      if (data[key].length > 4000) throw invalid(`Kolom ${key} terlalu panjang.`);
    }
    if (!data.sid || data.sid.length > 128) {
      summary.invalid_rows.push({ row: row.source_row, reason: "SID kosong atau terlalu panjang" });
      continue;
    }
    // Group first: a malformed duplicate must not let another row with that SID overwrite data.
    if (!bySid.has(data.sid)) bySid.set(data.sid, []);
    bySid.get(data.sid).push({ source_row: row.source_row, data });
  }
  const deviceInventory = prepareDeviceInventory(payload);
  const inventoryConflicts = new Map((deviceInventory?.conflicts || []).map(r => [r.sid.toUpperCase(), r]));
  const records = [];
  for (const [sid, rows] of bySid) {
    if (inventoryConflicts.has(sid.toUpperCase())) {
      if (!summary.conflicts.some(r => r.sid.toUpperCase() === sid.toUpperCase())) summary.conflicts.push(inventoryConflicts.get(sid.toUpperCase()));
      continue;
    }
    if (rows.length > 1) { summary.conflicts.push({ sid, rows: rows.map((row) => row.source_row) }); continue; }
    const record = rows[0];
    if (!record.data.customer_name || record.data.customer_name.length > 255) {
      summary.invalid_rows.push({ row: record.source_row, reason: "Nama pelanggan kosong atau terlalu panjang" });
      continue;
    }
    record.data.status = "OK";
    record.json = JSON.stringify(record.data);
    record.hash = crypto.createHash("sha256").update(record.json).digest("hex");
    records.push(record);
  }
  const institutions = prepareInstitutionInventory(payload);
  return { records, summary, snapshotAt, institutions, deviceInventory };
}

async function syncSnapshot(payload, database = pool) {
  const { records, summary, snapshotAt, institutions, deviceInventory } = prepareSnapshot(payload);
  const connection = await database.getConnection();
  try {
    await connection.beginTransaction();
    await connection.query("INSERT IGNORE INTO datin_sheet_sources (spreadsheet_id) VALUES (?)", [payload.spreadsheet_id]);
    const [[source]] = await connection.query("SELECT last_snapshot_at FROM datin_sheet_sources WHERE spreadsheet_id = ? FOR UPDATE", [payload.spreadsheet_id]);
    const [[previous]] = await connection.query("SELECT result_json FROM datin_sheet_sync_runs WHERE run_id = ?", [payload.run_id]);
    if (previous) {
      await connection.commit();
      return typeof previous.result_json === "string" ? JSON.parse(previous.result_json) : previous.result_json;
    }
    if (source.last_snapshot_at && snapshotAt <= new Date(source.last_snapshot_at)) throw invalid("Snapshot lama ditolak. Jalankan sinkronisasi baru.", 409);
    const [existing] = await connection.query("SELECT sid, data_hash FROM datin_customer_master WHERE spreadsheet_id = ?", [payload.spreadsheet_id]);
    const hashes = new Map(existing.map((row) => [row.sid, row.data_hash]));
    for (const record of records) {
      const hash = hashes.get(record.data.sid);
      summary[hash === undefined ? "created" : hash === record.hash ? "unchanged" : "updated"] += 1;
      await connection.query(`
        INSERT INTO datin_customer_master (spreadsheet_id, sid, source_row, customer_name, data_json, data_hash, last_synced_at, valid_snapshot_at)
        VALUES (?, ?, ?, ?, ?, ?, NOW(3), ?)
        ON DUPLICATE KEY UPDATE source_row = VALUES(source_row), customer_name = VALUES(customer_name),
          data_json = VALUES(data_json), data_hash = VALUES(data_hash), last_synced_at = NOW(3), valid_snapshot_at = VALUES(valid_snapshot_at)
      `, [payload.spreadsheet_id, record.data.sid, record.source_row, record.data.customer_name, record.json, record.hash, snapshotAt]);
    }
    let baseline = {};
    if (institutions) {
      const [[previousInventory]] = await connection.query(`SELECT result_json FROM datin_sheet_sync_runs
        WHERE spreadsheet_id = ? AND JSON_EXTRACT(result_json, '$.summary.institutions.guard_rows') IS NOT NULL
        ORDER BY snapshot_at DESC LIMIT 1`, [payload.spreadsheet_id]);
      if (previousInventory) {
        const storedResult = typeof previousInventory.result_json === "string" ? JSON.parse(previousInventory.result_json) : previousInventory.result_json;
        baseline = storedResult?.summary?.institutions || {};
      }
    }
    summary.institutions = await syncInstitutions(connection, institutions, records, baseline);
    summary.device_inventory = await saveDeviceInventory(connection, payload, deviceInventory, records, snapshotAt);
    const result = { success: true, run_id: payload.run_id, summary };
    await connection.query("INSERT INTO datin_sheet_sync_runs (run_id, spreadsheet_id, snapshot_at, result_json) VALUES (?, ?, ?, ?)", [payload.run_id, payload.spreadsheet_id, snapshotAt, JSON.stringify(result)]);
    await connection.query("UPDATE datin_sheet_sources SET last_snapshot_at = ? WHERE spreadsheet_id = ?", [snapshotAt, payload.spreadsheet_id]);
    await connection.commit();
    return result;
  } catch (error) {
    await connection.rollback();
    throw error;
  } finally {
    connection.release();
  }
}

module.exports = { prepareSnapshot, syncSnapshot };
