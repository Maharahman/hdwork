const pool = require("../config/database");
const SERVERS = { SBS: { tree_id: 34, base_url: "http://10.96.33.156/ont" }, SBU: { tree_id: 28, base_url: "http://10.100.2.234/ont" } };
const SOURCE_ID = "16tSELEZFzbH7FdI3NFqnNJx17xVf8GXgvZD5ghfxkLE";
const normalize = (value) => String(value || "").trim().replace(/\s+/g, " ").toUpperCase();
const fail = (message, statusCode = 400) => Object.assign(new Error(message), { statusCode });

function groupInstitutions(rows) {
  const groups = new Map();
  const skipped = [];
  for (const row of rows) {
    if (!row.sid) continue; // LEFT JOIN when snapshot has no accepted records.
    const data = typeof row.data_json === "string" ? JSON.parse(row.data_json) : row.data_json;
    const server = normalize(data.server);
    const name = normalize(data.customer_name);
    if (!SERVERS[server] || !name || normalize(data.status) !== "OK") {
      skipped.push({ sid: row.sid, source_row: row.source_row, reason: "Instansi, STATUS atau SERVER tidak valid" });
      continue;
    }
    const key = `${server}:${name}`;
    if (!groups.has(key)) groups.set(key, { server, name, ...SERVERS[server], customer_count: 0 });
    groups.get(key).customer_count += 1;
  }
  return { institutions: [...groups.values()].sort((a, b) => `${a.server}:${a.name}`.localeCompare(`${b.server}:${b.name}`)), skipped };
}

async function latestSourceRows(database = pool) {
  const spreadsheetId = process.env.DATIN_SHEET_ID || SOURCE_ID;
  const [rows] = await database.query(`
    SELECT s.last_snapshot_at, TIMESTAMPDIFF(SECOND, s.last_snapshot_at, NOW(3)) AS age_seconds,
      m.sid, m.source_row, m.data_json, r.result_json
    FROM datin_sheet_sources s
    LEFT JOIN datin_customer_master m ON m.spreadsheet_id = s.spreadsheet_id
      AND m.valid_snapshot_at = s.last_snapshot_at
    LEFT JOIN datin_sheet_sync_runs r ON r.spreadsheet_id = s.spreadsheet_id AND r.snapshot_at = s.last_snapshot_at
    WHERE s.spreadsheet_id = ?
  `, [spreadsheetId]);
  if (!rows.length || !rows[0].last_snapshot_at) throw fail("Belum ada snapshot spreadsheet. Jalankan sinkronisasi dahulu.", 409);
  const configuredAge = Number(process.env.DATIN_CACTI_SOURCE_MAX_AGE_SECONDS || 1800);
  const maxAge = Number.isFinite(configuredAge) && configuredAge > 0 ? configuredAge : 1800;
  if (Number(rows[0].age_seconds) > maxAge) throw fail("Snapshot spreadsheet kedaluwarsa. Periksa trigger sinkronisasi.", 409);
  return { spreadsheetId, rows };
}

async function getHeaderPlan(database = pool) {
  const { spreadsheetId, rows } = await latestSourceRows(database);
  return { success: true, spreadsheet_id: spreadsheetId, snapshot_at: rows[0].last_snapshot_at, ...groupInstitutions(rows) };
}

async function getDeviceSource(database = pool) {
  const { spreadsheetId, rows } = await latestSourceRows(database);
  const [aliasRows] = await database.query(`
    SELECT a.normalized_name, i.name AS institution_name
    FROM datin_institution_aliases a
    JOIN datin_institutions i ON i.id = a.institution_id AND i.is_active = 1
  `);
  const stored = rows[0].result_json;
  const run = typeof stored === "string" ? JSON.parse(stored) : stored;
  return {
    success: true, spreadsheet_id: spreadsheetId, snapshot_at: rows[0].last_snapshot_at,
    customers: rows.filter((row) => row.sid).map((row) => {
      const data = typeof row.data_json === "string" ? JSON.parse(row.data_json) : row.data_json;
      return { ...data, sid: row.sid, source_row: row.source_row };
    }),
    conflicts: run?.summary?.conflicts || [], invalid_rows: run?.summary?.invalid_rows || [],
    aliases: aliasRows.map((row) => ({ alias: normalize(row.normalized_name), name: normalize(row.institution_name) })),
  };
}

async function saveHeaderMappings(payload, database = pool) {
  if (!payload || !Array.isArray(payload.mappings) || payload.mappings.length > 2000) throw fail("Mapping header tidak valid.");
  const plan = await getHeaderPlan(database);
  if (payload.spreadsheet_id !== plan.spreadsheet_id || new Date(payload.snapshot_at).getTime() !== new Date(plan.snapshot_at).getTime()) {
    throw fail("Snapshot berubah. Ambil ulang rencana header.", 409);
  }
  const allowed = new Set(plan.institutions.map((item) => `${item.server}:${item.name}`));
  for (const mapping of payload.mappings) {
    if (!mapping || !SERVERS[mapping.server] || mapping.tree_id !== SERVERS[mapping.server].tree_id
      || !Number.isSafeInteger(mapping.header_id) || mapping.header_id < 1
      || typeof mapping.name !== "string" || mapping.name.length > 255
      || !allowed.has(`${mapping.server}:${normalize(mapping.name)}`)) throw fail("Mapping tidak sesuai instansi/server rencana.");
  }
  const connection = await database.getConnection();
  try {
    await connection.beginTransaction();
    for (const mapping of payload.mappings) {
      await connection.query(`
        INSERT INTO datin_cacti_headers (spreadsheet_id, server, tree_id, institution_name, normalized_name, header_id)
        VALUES (?, ?, ?, ?, ?, ?)
        ON DUPLICATE KEY UPDATE institution_name = VALUES(institution_name), header_id = VALUES(header_id), last_verified_at = NOW()
      `, [plan.spreadsheet_id, mapping.server, mapping.tree_id, mapping.name, normalize(mapping.name), mapping.header_id]);
    }
    await connection.commit();
    return { success: true, saved: payload.mappings.length };
  } catch (error) { await connection.rollback(); throw error; }
  finally { connection.release(); }
}

async function getDeviceMappings(database = pool) {
  const { spreadsheetId, rows } = await latestSourceRows(database);
  let mappings;
  try {
    [mappings] = await database.query(`
      SELECT sid, server, tree_id, host_id, header_id, ownership_status, verified_at
      FROM datin_cacti_devices WHERE spreadsheet_id = ?
    `, [spreadsheetId]);
  } catch (error) {
    if (error.code === "ER_NO_SUCH_TABLE") throw fail("Jalankan migrasi 20261019_add_datin_cacti_devices.sql terlebih dahulu.", 503);
    throw error;
  }
  return { success: true, spreadsheet_id: spreadsheetId, snapshot_at: rows[0].last_snapshot_at, mappings };
}

module.exports = { groupInstitutions, getHeaderPlan, getDeviceSource, saveHeaderMappings, getDeviceMappings };
