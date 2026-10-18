const crypto = require("crypto");
const pool = require("../config/database");
const { getDeviceSource } = require("./datin-cacti-headers.service");

// This release is restricted to the two explicitly approved pilot targets.
const TARGETS = Object.freeze({
  SBS: { sid: "1165651712", tree_id: 34, header_id: 9127 },
  SBU: { sid: "1015518858", tree_id: 28, header_id: 2929 },
});
const FIELDS = ["olt_ip", "slot", "port", "onu", "serial_number"];
const fail = (message, statusCode = 400) => Object.assign(new Error(message), { statusCode });
const hash = (value) => crypto.createHash("sha256").update(value).digest("hex");
const json = (value) => typeof value === "string" ? JSON.parse(value) : value;
const text = (value) => String(value ?? "").trim();

function desiredDevice(row) {
  if (text(row.status).toUpperCase() !== "OK") throw fail("STATUS sumber wajib OK.", 409);
  const desired = Object.fromEntries(FIELDS.map((field) => [field, text(row[field])]));
  if (FIELDS.some((field) => !desired[field]) || ["slot", "port", "onu"].some((field) => !/^\d{1,3}$/.test(desired[field]))) {
    throw fail("Konfigurasi ONT kosong/tidak valid atau melebihi batas form.", 409);
  }
  if (!require("net").isIP(desired.olt_ip) || desired.serial_number.length > 50) throw fail("IP OLT atau SN tidak valid.", 409);
  if (text(row.customer_name).replace(/\s+/g, " ").toUpperCase() !== "BANK JATIM") throw fail("Instansi pilot berubah; perlu persetujuan ulang.", 409);
  desired.description = "[HD-D] " + [row.sid, row.customer_name, row.customer_detail].map(text).filter(Boolean).join("_");
  if (desired.description.length > 250) throw fail("Description melebihi batas form.", 409);
  return desired;
}

function pilotCapabilities() {
  return { success: true, version: 1, targets: TARGETS, automatic_retry: false, bulk_enabled: false };
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
    if (error.code === "ER_DUP_ENTRY") throw fail("Reservasi/mapping sudah ada. Jangan ulang create; periksa hasil sebelumnya.", 409);
    if (error.code === "ER_NO_SUCH_TABLE") throw fail("Migrasi tabel pilot/mapping belum terpasang.", 503);
    throw error;
  } finally { connection.release(); }
}

async function reservePilot(payload, database = pool) {
  const target = TARGETS[payload?.server];
  if (!target || payload.sid !== target.sid || payload.tree_id !== target.tree_id || payload.header_id !== target.header_id) {
    throw fail("Target bukan salah satu dari dua pilot yang disetujui.");
  }
  const source = await getDeviceSource(database);
  if (payload.spreadsheet_id !== source.spreadsheet_id || new Date(payload.snapshot_at).getTime() !== new Date(source.snapshot_at).getTime()) {
    throw fail("Snapshot berubah; ambil ulang pratinjau.", 409);
  }
  const rows = source.customers.filter((row) => row.sid === target.sid && text(row.server).toUpperCase() === payload.server);
  if (rows.length !== 1 || source.conflicts.some((item) => item.sid === target.sid)) throw fail("SID pilot tidak tersedia unik pada snapshot.", 409);
  const desired = desiredDevice(rows[0]);
  if (!payload.desired || Object.keys(desired).some((key) => payload.desired[key] !== desired[key])) throw fail("Data pratinjau berubah; persetujuan ulang diperlukan.", 409);
  return transaction(database, async (connection) => {
    // Lock the accepted master row while checking the current snapshot again.
    const [current] = await connection.query(`
      SELECT m.data_json, s.last_snapshot_at FROM datin_sheet_sources s
      JOIN datin_customer_master m ON m.spreadsheet_id = s.spreadsheet_id AND m.valid_snapshot_at = s.last_snapshot_at
      WHERE s.spreadsheet_id = ? AND m.sid = ? FOR UPDATE
    `, [source.spreadsheet_id, target.sid]);
    if (current.length !== 1 || new Date(current[0].last_snapshot_at).getTime() !== new Date(source.snapshot_at).getTime()
        || JSON.stringify(desiredDevice({ ...json(current[0].data_json), sid: target.sid })) !== JSON.stringify(desired)) {
      throw fail("Sumber berubah sebelum reservasi; ulangi pratinjau.", 409);
    }
    const [existing] = await connection.query("SELECT host_id FROM datin_cacti_devices WHERE spreadsheet_id = ? AND sid = ? FOR UPDATE", [source.spreadsheet_id, target.sid]);
    if (existing.length) throw fail("SID sudah mempunyai mapping; create ditolak.", 409);
    const id = crypto.randomUUID(), token = crypto.randomBytes(32).toString("hex");
    await connection.query(`INSERT INTO datin_cacti_pilot_jobs
      (id, spreadsheet_id, sid, server, tree_id, header_id, token_hash, desired_json)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)`, [id, source.spreadsheet_id, target.sid, payload.server, target.tree_id, target.header_id, hash(token), JSON.stringify(desired)]);
    return { success: true, job_id: id, token, desired, ...target, server: payload.server, state: "RESERVED" };
  });
}

async function completePilot(payload, database = pool) {
  if (!payload || !/^[0-9a-f-]{36}$/i.test(payload.job_id || "") || !/^[0-9a-f]{64}$/.test(payload.token || "")
      || !Number.isSafeInteger(payload.host_id) || payload.host_id < 1) throw fail("Bukti pilot tidak valid.");
  return transaction(database, async (connection) => {
    const [jobs] = await connection.query("SELECT * FROM datin_cacti_pilot_jobs WHERE id = ? FOR UPDATE", [payload.job_id]);
    const job = jobs[0];
    if (!job || !crypto.timingSafeEqual(Buffer.from(job.token_hash), Buffer.from(hash(payload.token)))) throw fail("Reservasi pilot tidak dikenali.", 403);
    const desired = json(job.desired_json), receipt = payload.receipt;
    if (!receipt || Object.keys(desired).some((key) => receipt[key] !== desired[key])
        || receipt.server !== job.server || receipt.tree_id !== job.tree_id || receipt.header_id !== job.header_id
        || receipt.host_id !== payload.host_id || receipt.monitor !== true || receipt.disabled !== false
        || receipt.host_template_id !== "0" || receipt.snmp_version !== "0" || receipt.availability_method !== "3" || receipt.ping_method !== "1") {
      throw fail("Hasil read-back device/tree belum sesuai reservasi.", 409);
    }
    // Receipt is an attestation from the authenticated VPN agent, not independently fetched by HD Work.
    if (job.job_status === "VERIFIED") {
      if (job.host_id !== payload.host_id) throw fail("Host ID hasil sebelumnya berbeda.", 409);
      return { success: true, state: "VERIFIED", host_id: job.host_id, replay: true };
    }
    await connection.query(`INSERT INTO datin_cacti_devices
      (spreadsheet_id, sid, server, tree_id, host_id, header_id, ownership_status, verified_at)
      VALUES (?, ?, ?, ?, ?, ?, 'VERIFIED', NOW())`, [job.spreadsheet_id, job.sid, job.server, job.tree_id, payload.host_id, job.header_id]);
    await connection.query("UPDATE datin_cacti_pilot_jobs SET job_status = 'VERIFIED', host_id = ?, verified_at = NOW() WHERE id = ?", [payload.host_id, job.id]);
    return { success: true, state: "VERIFIED", host_id: payload.host_id, replay: false };
  });
}

module.exports = { pilotCapabilities, desiredDevice, reservePilot, completePilot };
