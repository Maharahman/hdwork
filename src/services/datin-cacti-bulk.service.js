const crypto = require("crypto");
const pool = require("../config/database");
const { getDeviceSource } = require("./datin-cacti-headers.service");
const { checkSource, digest } = require("./datin-cacti-batch.service");
const fail = (message, statusCode = 409) => Object.assign(new Error(message), { statusCode });
const hash = (s) => crypto.createHash("sha256").update(s).digest("hex");
const parse = (s) => typeof s === "string" ? JSON.parse(s) : s;
const jobToken = (token, sid) => crypto.createHmac("sha256", token).update(sid).digest("hex");

function validateBulk(payload) {
  if (!/^bulk-[0-9a-f-]{36}$/.test(payload?.batch_id || "") || !/^[0-9a-f]{64}$/.test(payload?.token || "")) throw fail("Identitas bulk tidak valid.", 400);
  const rows = payload.manifest;
  if (!Array.isArray(rows) || !rows.length || rows.length > 1000 || new Set(rows.map(r => r?.sid)).size !== rows.length) throw fail("Bulk wajib 1–1000 SID unik.", 400);
  const headers = new Map();
  for (const r of rows) {
    if (!r || typeof r.sid !== "string" || !r.sid || r.sid.length > 128 || !["SBS", "SBU"].includes(r.server) || r.tree_id !== { SBS: 34, SBU: 28 }[r.server]
      || typeof r.institution !== "string" || !r.institution || r.institution.length > 255 || r.institution === "ATCS"
      || !r.desired || typeof r.description !== "string" || !r.creation_profile
      || !(r.header_id === null || (Number.isSafeInteger(r.header_id) && r.header_id > 0))) throw fail("Target bulk tidak valid/dilindungi.", 400);
    const key = `${r.server}:${r.institution}`;
    if (headers.has(key) && headers.get(key) !== r.header_id) throw fail("Identitas header tidak konsisten.", 400);
    headers.set(key, r.header_id);
  }
  if (digest(rows) !== payload.manifest_sha256) throw fail("Hash rencana bulk berbeda.");
  return rows;
}

// The agent durably saves its random token BEFORE requesting a reservation.
// Deterministic job tokens allow an identical replay after a lost HTTP response.
async function reserveBulk(payload, database = pool) {
  const manifest = validateBulk(payload), connection = await database.getConnection();
  try {
    await connection.beginTransaction();
    await connection.query("SELECT spreadsheet_id FROM datin_sheet_sources WHERE spreadsheet_id = ? FOR UPDATE", [payload.spreadsheet_id]);
    const [previous] = await connection.query("SELECT * FROM datin_cacti_batch_runs WHERE batch_id = ? FOR UPDATE", [payload.batch_id]);
    if (previous.length) {
      if (previous[0].spreadsheet_id !== payload.spreadsheet_id || previous[0].token_hash !== hash(payload.token)
          || digest(parse(previous[0].manifest_json)) !== payload.manifest_sha256) throw fail("Replay bulk tidak sama.");
    } else {
      const source = await getDeviceSource(connection);
      if (payload.spreadsheet_id !== source.spreadsheet_id || new Date(payload.snapshot_at).getTime() !== new Date(source.snapshot_at).getTime()) throw fail("Snapshot berubah; ulangi preflight.");
      const aliases = new Map(source.aliases.map(r => [r.alias, r.name]));
      const desired = manifest.map(r => checkSource(r, source, aliases));
      await connection.query("INSERT INTO datin_cacti_batch_runs (batch_id, spreadsheet_id, token_hash, manifest_json) VALUES (?, ?, ?, ?)", [payload.batch_id, source.spreadsheet_id, hash(payload.token), JSON.stringify(manifest)]);
      const headers = [...new Map(manifest.map(r => [`${r.server}:${r.institution}`, r])).values()];
      for (const r of headers) {
        const [mapped] = await connection.query("SELECT header_id FROM datin_cacti_headers WHERE spreadsheet_id = ? AND server = ? AND tree_id = ? AND normalized_name = ? FOR UPDATE", [source.spreadsheet_id, r.server, r.tree_id, r.institution]);
        if (r.header_id === null) {
          if (mapped.length) throw fail("Header terpetakan; jangan membuat pengganti.");
          await connection.query("INSERT INTO datin_cacti_batch_headers (batch_id, spreadsheet_id, server, institution, tree_id) VALUES (?, ?, ?, ?, ?)", [payload.batch_id, source.spreadsheet_id, r.server, r.institution, r.tree_id]);
        } else {
          if (mapped.length && mapped[0].header_id !== r.header_id) throw fail("Mapping header berbeda dari bukti agent.");
          // Existing header is an authenticated agent attestation, not host adoption.
          if (!mapped.length) await connection.query("INSERT INTO datin_cacti_headers (spreadsheet_id, server, tree_id, institution_name, normalized_name, header_id) VALUES (?, ?, ?, ?, ?, ?)", [source.spreadsheet_id, r.server, r.tree_id, r.institution, r.institution, r.header_id]);
        }
      }
      for (let i = 0; i < manifest.length; i++) {
        const r = manifest[i];
        const [mapped] = await connection.query("SELECT host_id FROM datin_cacti_devices WHERE spreadsheet_id = ? AND sid = ? FOR UPDATE", [source.spreadsheet_id, r.sid]);
        if (mapped.length) throw fail("SID sudah terpetakan; create ditolak.");
        await connection.query("INSERT INTO datin_cacti_pilot_jobs (id, spreadsheet_id, sid, server, tree_id, header_id, token_hash, desired_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [crypto.randomUUID(), source.spreadsheet_id, r.sid, r.server, r.tree_id, r.header_id || 0, hash(jobToken(payload.token, r.sid)), JSON.stringify(desired[i])]);
      }
    }
    const jobs = [];
    for (const r of manifest) {
      const [found] = await connection.query("SELECT id, server, header_id, job_status, host_id FROM datin_cacti_pilot_jobs WHERE spreadsheet_id = ? AND sid = ?", [payload.spreadsheet_id, r.sid]);
      if (found.length !== 1) throw fail("Reservasi device tidak lengkap.");
      jobs.push({ ...found[0], job_id: found[0].id, sid: r.sid, token: jobToken(payload.token, r.sid) });
    }
    await connection.commit();
    return { success: true, batch_id: payload.batch_id, jobs };
  } catch (e) {
    await connection.rollback();
    if (e.code === "ER_DUP_ENTRY") throw fail("SID/header sudah direservasi pekerjaan lain; jangan ulang create.");
    if (e.code === "ER_NO_SUCH_TABLE") throw fail("Migrasi batch/pilot/devices belum terpasang.", 503);
    throw e;
  } finally { connection.release(); }
}
module.exports = { validateBulk, reserveBulk };
