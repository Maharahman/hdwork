const crypto = require("crypto");
const pool = require("../config/database");
const config = require("../config/datin-cacti-batch.json");
const { getDeviceSource } = require("./datin-cacti-headers.service");
const { desiredDevice } = require("./datin-cacti-pilot.service");
const fail = (message, statusCode = 400) => Object.assign(new Error(message), { statusCode });
const parse = (value) => typeof value === "string" ? JSON.parse(value) : value;
const normalize = (value) => String(value ?? "").trim().replace(/\s+/g, " ").toUpperCase();
const hash = (value) => crypto.createHash("sha256").update(value).digest("hex");
const order = (value) => Array.isArray(value) ? value.map(order) : value && typeof value === "object"
  ? Object.fromEntries(Object.keys(value).sort().map((key) => [key, order(value[key])])) : value;
const digest = (value) => hash(JSON.stringify(order(value)));
const sortRows = (rows) => [...rows].sort((a, b) => `${a.server}:${a.sid}` < `${b.server}:${b.sid}` ? -1 : `${a.server}:${a.sid}` > `${b.server}:${b.sid}` ? 1 : 0);

function validateManifest(manifest, settings = config) {
  if (!Array.isArray(manifest) || manifest.length !== settings.limit || manifest.some((row) => !row || typeof row !== "object") || new Set(manifest.map((row) => row.sid)).size !== settings.limit) throw fail("Batch wajib tepat 10 SID unik yang disetujui.");
  const rows = sortRows(manifest);
  for (const row of rows) {
    const target = settings.targets.find((item) => item.sid === row.sid);
    if (!target || Object.keys(target).some((key) => target[key] !== row[key])) throw fail("SID/server/instansi bukan target batch yang disetujui.");
  }
  if (digest(rows) !== settings.approved_sha256) throw fail("Konfigurasi batch berbeda dari manifest yang disetujui.", 409);
  return rows;
}

function checkSource(row, source, aliases) {
  const matches = source.customers.filter((item) => item.sid === row.sid && normalize(item.server) === row.server);
  if (matches.length !== 1 || source.conflicts.some((item) => item.sid === row.sid)) throw fail("SID batch tidak unik/konflik pada sumber.", 409);
  const data = matches[0], name = normalize(data.customer_name);
  if ((aliases.get(name) || name) !== row.institution || row.institution === "ATCS") throw fail("Instansi sumber berubah/dilindungi.", 409);
  const desired = desiredDevice(data, name);
  if (row.description !== desired.description || Object.keys(row.desired).length !== 5 || Object.keys(desired).some((key) => key !== "description" && row.desired[key] !== desired[key])) {
    throw fail("Konfigurasi sumber berubah dari batch yang disetujui.", 409);
  }
  return desired;
}

async function transaction(database, work) {
  const connection = await database.getConnection();
  try {
    await connection.beginTransaction();
    const result = await work(connection);
    await connection.commit();
    return result;
  } catch (error) {
    await connection.rollback();
    if (error.code === "ER_DUP_ENTRY") throw fail("Batch/SID/header sudah direservasi. Jangan ulang create.", 409);
    if (error.code === "ER_NO_SUCH_TABLE") throw fail("Migrasi batch/mapping belum terpasang.", 503);
    throw error;
  } finally { connection.release(); }
}

function batchCapabilities() {
  return { success: true, version: 1, ...config, automatic_retry: false, unrestricted_bulk: false };
}

async function reserveBatch(payload, database = pool, settings = config) {
  if (payload?.batch_id !== settings.batch_id) throw fail("Batch tidak dikenali.");
  const manifest = validateManifest(payload.manifest, settings);
  const source = await getDeviceSource(database);
  if (payload.spreadsheet_id !== source.spreadsheet_id || new Date(payload.snapshot_at).getTime() !== new Date(source.snapshot_at).getTime()) throw fail("Snapshot berubah; ulangi preflight.", 409);
  const aliases = new Map(source.aliases.map((row) => [row.alias, row.name]));
  const desired = manifest.map((row) => checkSource(row, source, aliases));
  const dependencies = [...new Map(manifest.map((row) => [`${row.server}:${row.institution}`, { server: row.server, tree_id: row.tree_id, institution: row.institution }])).values()];
  return transaction(database, async (connection) => {
    const token = crypto.randomBytes(32).toString("hex");
    // Unique batch ID and header keys remain locked after crashes/timeouts.
    await connection.query("INSERT INTO datin_cacti_batch_runs (batch_id, spreadsheet_id, token_hash, manifest_json) VALUES (?, ?, ?, ?)", [settings.batch_id, source.spreadsheet_id, hash(token), JSON.stringify(manifest)]);
    for (const header of dependencies) {
      const [mappedHeaders] = await connection.query(`SELECT header_id FROM datin_cacti_headers
        WHERE spreadsheet_id = ? AND server = ? AND tree_id = ? AND normalized_name = ? FOR UPDATE`, [source.spreadsheet_id, header.server, header.tree_id, header.institution]);
      if (mappedHeaders.length) throw fail("Header memiliki mapping lama; tinjau sebelum membuat pengganti.", 409);
      await connection.query("INSERT INTO datin_cacti_batch_headers (batch_id, spreadsheet_id, server, institution, tree_id) VALUES (?, ?, ?, ?, ?)", [settings.batch_id, source.spreadsheet_id, header.server, header.institution, header.tree_id]);
    }
    const jobs = [];
    for (let index = 0; index < manifest.length; index++) {
      const row = manifest[index];
      const [current] = await connection.query(`SELECT m.data_json, s.last_snapshot_at FROM datin_sheet_sources s
        JOIN datin_customer_master m ON m.spreadsheet_id = s.spreadsheet_id AND m.valid_snapshot_at = s.last_snapshot_at
        WHERE s.spreadsheet_id = ? AND m.sid = ? FOR UPDATE`, [source.spreadsheet_id, row.sid]);
      if (current.length !== 1 || new Date(current[0].last_snapshot_at).getTime() !== new Date(source.snapshot_at).getTime()) throw fail("Snapshot berubah sebelum reservasi batch.", 409);
      checkSource(row, { customers: [{ ...parse(current[0].data_json), sid: row.sid }], conflicts: [] }, aliases);
      const [existing] = await connection.query("SELECT host_id FROM datin_cacti_devices WHERE spreadsheet_id = ? AND sid = ? FOR UPDATE", [source.spreadsheet_id, row.sid]);
      if (existing.length) throw fail("SID batch sudah terpetakan; create ditolak.", 409);
      const jobId = crypto.randomUUID(), jobToken = crypto.randomBytes(32).toString("hex");
      await connection.query(`INSERT INTO datin_cacti_pilot_jobs
        (id, spreadsheet_id, sid, server, tree_id, header_id, token_hash, desired_json)
        VALUES (?, ?, ?, ?, ?, 0, ?, ?)`, [jobId, source.spreadsheet_id, row.sid, row.server, row.tree_id, hash(jobToken), JSON.stringify(desired[index])]);
      jobs.push({ job_id: jobId, token: jobToken, sid: row.sid, server: row.server });
    }
    return { success: true, batch_id: settings.batch_id, token, jobs, headers: dependencies, state: "RESERVED" };
  });
}

async function authenticateBatch(connection, payload) {
  if (payload?.batch_id !== config.batch_id || !/^[0-9a-f]{64}$/.test(payload?.token || "")) throw fail("Bukti batch tidak valid.", 403);
  const [rows] = await connection.query("SELECT * FROM datin_cacti_batch_runs WHERE batch_id = ? FOR UPDATE", [payload.batch_id]);
  const batch = rows[0];
  if (!batch || !crypto.timingSafeEqual(Buffer.from(batch.token_hash), Buffer.from(hash(payload.token)))) throw fail("Reservasi batch tidak dikenali.", 403);
  return batch;
}

async function bindBatchHeader(payload, database = pool) {
  if (!Number.isSafeInteger(payload?.header_id) || payload.header_id < 1) throw fail("Header ID tidak valid.");
  return transaction(database, async (connection) => {
    const batch = await authenticateBatch(connection, payload);
    const targets = parse(batch.manifest_json).filter((row) => row.server === payload.server && row.institution === payload.institution && row.tree_id === payload.tree_id);
    if (!targets.length) throw fail("Header di luar manifest.");
    const [headers] = await connection.query("SELECT * FROM datin_cacti_batch_headers WHERE batch_id = ? AND server = ? AND institution = ? FOR UPDATE", [payload.batch_id, payload.server, payload.institution]);
    const header = headers[0];
    if (!header) throw fail("Header tidak direservasi.", 409);
    if (header.header_status === "VERIFIED") {
      if (header.header_id !== payload.header_id) throw fail("Header hasil sebelumnya berbeda.", 409);
      return { success: true, header_id: header.header_id, replay: true };
    }
    await connection.query(`INSERT INTO datin_cacti_headers (spreadsheet_id, server, tree_id, institution_name, normalized_name, header_id)
      VALUES (?, ?, ?, ?, ?, ?)`, [batch.spreadsheet_id, payload.server, payload.tree_id, payload.institution, payload.institution, payload.header_id]);
    for (const row of targets) {
      const [result] = await connection.query(`UPDATE datin_cacti_pilot_jobs SET header_id = ?
        WHERE spreadsheet_id = ? AND sid = ? AND server = ? AND header_id = 0 AND job_status = 'RESERVED'`, [payload.header_id, batch.spreadsheet_id, row.sid, row.server]);
      if (result.affectedRows !== 1) throw fail("Reservasi device/header sudah berubah.", 409);
    }
    await connection.query("UPDATE datin_cacti_batch_headers SET header_status = 'VERIFIED', header_id = ? WHERE batch_id = ? AND server = ? AND institution = ?", [payload.header_id, payload.batch_id, payload.server, payload.institution]);
    return { success: true, header_id: payload.header_id, replay: false };
  });
}

async function finishBatch(payload, database = pool) {
  return transaction(database, async (connection) => {
    const batch = await authenticateBatch(connection, payload);
    for (const row of parse(batch.manifest_json)) {
      const [mappings] = await connection.query(`SELECT d.host_id FROM datin_cacti_devices d JOIN datin_cacti_pilot_jobs j
        ON j.spreadsheet_id = d.spreadsheet_id AND j.sid = d.sid AND j.host_id = d.host_id AND j.header_id = d.header_id
        WHERE d.spreadsheet_id = ? AND d.sid = ? AND d.server = ? AND d.tree_id = ?
          AND d.ownership_status = 'VERIFIED' AND j.job_status = 'VERIFIED'`, [batch.spreadsheet_id, row.sid, row.server, row.tree_id]);
      if (mappings.length !== 1) throw fail("Belum semua device batch terverifikasi.", 409);
    }
    await connection.query("UPDATE datin_cacti_batch_runs SET run_status = 'VERIFIED', verified_at = NOW() WHERE batch_id = ?", [payload.batch_id]);
    return { success: true, state: "VERIFIED", devices: config.limit };
  });
}

module.exports = { digest, validateManifest, checkSource, batchCapabilities, reserveBatch, bindBatchHeader, finishBatch };
