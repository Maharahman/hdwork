const crypto = require("crypto");
const pool = require("../config/database");
const { getDeviceSource } = require("./datin-cacti-headers.service");
const { desiredDevice } = require("./datin-cacti-pilot.service");
const { digest } = require("./datin-cacti-batch.service");
const parse = v => typeof v === "string" ? JSON.parse(v) : v;
const norm = v => String(v ?? "").trim().replace(/\s+/g, " ").toUpperCase();
const hash = v => crypto.createHash("sha256").update(v).digest("hex");
const fail = (message, statusCode = 409) => Object.assign(new Error(message), { statusCode });
const uuid = v => /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(v || "");
const enabled = () => process.env.DATIN_CACTI_AUTOMATION_ENABLED === "1";
const sheetId = () => process.env.DATIN_SHEET_ID || "16tSELEZFzbH7FdI3NFqnNJx17xVf8GXgvZD5ghfxkLE";
const deleteLimit = () => Math.min(100, Math.max(1, Number(process.env.DATIN_CACTI_MAX_DELETES_PER_RUN) || 10));
const same = (a, b) => digest(a) === digest(b);
const active = m => m && m.sync_state !== "DELETED" && m.ownership_status === "VERIFIED" && m.verified_at;
const identity = m => m ? Object.fromEntries(["sid", "server", "tree_id", "header_id", "host_id", "sync_state", "ownership_status"].map(k => [k, m[k]])) : null;

function canonical(source, name) {
  return source.aliases.find(a => a.alias === norm(name))?.name || norm(name);
}
function statusRow(source, sid) {
  const rows = source.inventory.rows.filter(r => norm(r.sid) === norm(sid));
  if (rows.length !== 1 || rows[0].sid !== sid) throw fail("SID hilang/konflik; tidak boleh disimpulkan NOT.");
  return rows[0];
}
function desired(source, sid) {
  const status = statusRow(source, sid), rows = source.customers.filter(r => r.sid === sid);
  if (status.status !== "OK" || rows.length !== 1 || source.conflicts.some(r => norm(r.sid) === norm(sid))) throw fail("SID bukan OK unik pada sumber terbaru.");
  if (canonical(source, status.customer_name) === "ATCS") throw fail("ATCS dilindungi.");
  const row = rows[0], server = norm(row.server), tree_id = { SBS: 34, SBU: 28 }[server];
  if (!tree_id) throw fail("Server sumber tidak valid.");
  const config = desiredDevice(row, norm(row.customer_name));
  for (const k of ["slot", "port", "onu"]) config[k] = String(Number(config[k]));
  return { server, tree_id, institution: canonical(source, row.customer_name), config };
}
function mappedHeader(source, server, institution) {
  const entries = source.headers.filter(r => r.server === server && canonical(source, r.normalized_name) === institution);
  const ids = [...new Set(entries.map(r => r.header_id))];
  if (ids.length > 1) throw fail("Mapping header instansi ambigu.");
  return ids[0] || null;
}
function deletionEligible(source, sid, mapping) {
  const row = statusRow(source, sid);
  if (row.status !== "NOT" || !source.inventory.safe_for_delete || !["SBS", "SBU"].includes(row.server)
      || !row.customer_name || canonical(source, row.customer_name) === "ATCS") throw fail("Bukti NOT tidak lengkap/aman.");
  if (active(mapping) && (mapping.server !== row.server || mapping.tree_id !== { SBS: 34, SBU: 28 }[row.server]
      || mappedHeader(source, mapping.server, canonical(source, row.customer_name)) !== mapping.header_id)) throw fail("Server/header NOT berbeda dari mapping; perlu review.");
  const candidates = source.mappings.filter(m => active(m) && source.inventory.rows.filter(r => norm(r.sid) === norm(m.sid)).length === 1
    && source.inventory.rows.find(r => r.sid === m.sid)?.status === "NOT");
  if (candidates.length > deleteLimit()) throw fail("Jumlah delete melebihi batas per run; periksa spreadsheet sebelum menaikkan batas.");
}

async function getSyncSource(database = pool) {
  try {
    const source = await getDeviceSource(database);
    const [[state]] = await database.query("SELECT device_inventory_json, device_inventory_snapshot_at, last_snapshot_at FROM datin_sheet_sources WHERE spreadsheet_id = ?", [source.spreadsheet_id]);
    const inventory = parse(state?.device_inventory_json);
    if (!inventory?.complete || inventory.version !== 1 || new Date(state.device_inventory_snapshot_at).getTime() !== new Date(source.snapshot_at).getTime()
        || new Date(state.last_snapshot_at).getTime() !== new Date(source.snapshot_at).getTime()) throw fail("Perbarui Apps Script dan sinkronkan snapshot SID/status lengkap terlebih dahulu.");
    const [mappings] = await database.query("SELECT sid, server, tree_id, host_id, header_id, ownership_status, verified_at, sync_state, last_applied_json FROM datin_cacti_devices WHERE spreadsheet_id = ?", [source.spreadsheet_id]);
    const [headers] = await database.query("SELECT server, tree_id, normalized_name, header_id FROM datin_cacti_headers WHERE spreadsheet_id = ?", [source.spreadsheet_id]);
    const [masters] = await database.query("SELECT sid, source_status FROM datin_customer_master WHERE spreadsheet_id = ?", [source.spreadsheet_id]);
    return { ...source, inventory, max_deletes_per_run: deleteLimit(), mappings: mappings.map(m => ({ ...m, last_applied_json: parse(m.last_applied_json) })), headers, masters };
  } catch (e) {
    if (["ER_NO_SUCH_TABLE", "ER_BAD_FIELD_ERROR"].includes(e.code)) throw fail("Jalankan migrasi 20261019_add_datin_cacti_reconcile.sql.", 503);
    throw e;
  }
}

async function tx(database, fn) {
  if (!enabled()) throw fail("Otomatisasi belum diaktifkan di backend.", 503);
  const c = await database.getConnection();
  try {
    await c.beginTransaction();
    await c.query("SELECT spreadsheet_id FROM datin_sheet_sources WHERE spreadsheet_id = ? FOR UPDATE", [sheetId()]);
    const result = await fn(c);
    await c.commit();
    return result;
  } catch (e) {
    await c.rollback();
    if (e.code === "ER_DUP_ENTRY") throw fail("Run/resource sudah direservasi; gunakan jurnal yang sama, jangan membuat ulang.");
    if (["ER_NO_SUCH_TABLE", "ER_BAD_FIELD_ERROR"].includes(e.code)) throw fail("Migrasi reconcile belum terpasang.", 503);
    throw e;
  } finally { c.release(); }
}
function credentials(p) {
  if (!uuid(p?.run_id) || !/^[0-9a-f]{64}$/.test(p?.token || "")) throw fail("Identitas run tidak valid.", 400);
}
async function auth(c, p) {
  credentials(p);
  const [[run]] = await c.query("SELECT * FROM datin_cacti_sync_runs WHERE id = ? FOR UPDATE", [p.run_id]);
  if (!run || run.spreadsheet_id !== sheetId() || !crypto.timingSafeEqual(Buffer.from(run.token_hash), Buffer.from(hash(p.token)))) throw fail("Run/token tidak dikenali.", 403);
  await c.query("UPDATE datin_cacti_sync_runs SET heartbeat_at = NOW(3) WHERE id = ?", [run.id]);
  return run;
}
async function startRun(p, database = pool) {
  credentials(p);
  return tx(database, async c => {
    const [[previous]] = await c.query("SELECT * FROM datin_cacti_sync_runs WHERE id = ? FOR UPDATE", [p.run_id]);
    if (previous) { await auth(c, p); return { success: true, state: previous.run_status, replay: true }; }
    const source = await getSyncSource(c);
    if (p.spreadsheet_id !== source.spreadsheet_id) throw fail("Sumber run berbeda.");
    const [pending] = await c.query("SELECT id FROM datin_cacti_pilot_jobs WHERE spreadsheet_id = ? AND job_status <> 'VERIFIED' LIMIT 1", [source.spreadsheet_id]);
    if (pending.length) throw fail("Ada reservasi pilot/bulk lama yang belum selesai; rekonsiliasi dahulu.");
    await c.query("INSERT INTO datin_cacti_sync_runs (id, spreadsheet_id, token_hash) VALUES (?, ?, ?)", [p.run_id, source.spreadsheet_id, hash(p.token)]);
    return { success: true, state: "RUNNING", replay: false };
  });
}

function expectedFor(source, p) {
  const mapping = source.mappings.find(m => m.sid === p.sid) || null;
  const operation = p.operation;
  if (operation === "HEADER") {
    const { server, tree_id, institution, header_id } = p.observed || {};
    if (tree_id !== { SBS: 34, SBU: 28 }[server] || !["SBS", "SBU"].includes(server) || institution === "ATCS"
        || typeof institution !== "string" || !(header_id === null || (Number.isSafeInteger(header_id) && header_id > 0))
        || !source.customers.some(r => norm(r.server) === server && canonical(source, r.customer_name) === institution)) throw fail("Header di luar instansi/server OK.");
    const stored = mappedHeader(source, server, institution);
    if (stored && stored !== header_id) throw fail("Header mapping hilang/berubah; tidak membuat pengganti.");
    return { operation, server, tree_id, institution, header_id };
  }
  if (typeof p.sid !== "string" || !p.sid || p.sid.length > 128) throw fail("SID tidak valid.", 400);
  if (["CREATE", "UPDATE", "VERIFY"].includes(operation)) {
    const target = desired(source, p.sid);
    if (!p.desired || !same(p.desired, target.config)) throw fail("Konfigurasi sumber berubah.");
    const header_id = mappedHeader(source, target.server, target.institution);
    if (!header_id || p.observed?.header_id !== header_id) throw fail("Header belum diverifikasi.");
    if (operation === "CREATE") {
      if (mapping && mapping.sync_state !== "DELETED") throw fail("Mapping aktif/tidak terpercaya; jangan create/adopsi.");
      if (p.observed?.absent !== true || p.observed?.inventory_complete !== true) throw fail("Bukti inventaris create tidak lengkap.");
    } else {
      if (!active(mapping) || mapping.server !== target.server || mapping.tree_id !== target.tree_id || mapping.header_id !== header_id
          || !same(p.observed?.mapping, identity(mapping)) || !p.observed?.config || !p.observed?.option_guard) throw fail("Mapping/server/tree/header edit berubah atau bukti kosong.");
    }
    return { operation, ...target, header_id, mapping: identity(mapping) };
  }
  if (["DELETE", "CLEANUP"].includes(operation)) {
    deletionEligible(source, p.sid, mapping);
    if (operation === "DELETE" && (!active(mapping) || !same(p.observed?.mapping, identity(mapping)) || p.observed?.inventory_complete !== true
        || (p.observed?.absent !== true && (!p.observed?.config || !p.observed?.option_guard || p.observed?.exclusive_tree !== true)))) throw fail("Delete tanpa mapping VERIFIED/bukti host eksklusif ditolak.");
    if (operation === "CLEANUP" && mapping && mapping.sync_state !== "DELETED") throw fail("Host belum diverifikasi terhapus; jangan bersihkan master.");
    return { operation, mapping: identity(mapping), row: statusRow(source, p.sid) };
  }
  throw fail("Operasi tidak dikenali.", 400);
}

function jobResult(job) {
  return { success: true, job_id: job.id, state: job.job_status, expected: parse(job.expected_json), request: parse(job.request_json), receipt: parse(job.receipt_json) };
}
async function prepareJob(p, database = pool) {
  if (!uuid(p?.job_id)) throw fail("Job ID tidak valid.", 400);
  const request = { sid: p.sid || null, operation: p.operation, observed: p.observed || null, desired: p.desired || null };
  return tx(database, async c => {
    const run = await auth(c, p);
    const [[old]] = await c.query("SELECT * FROM datin_cacti_sync_jobs WHERE id = ? FOR UPDATE", [p.job_id]);
    if (old) {
      if (old.run_id !== run.id || old.request_hash !== digest(request)) throw fail("Replay job berubah.");
      return jobResult(old);
    }
    if (run.run_status !== "RUNNING") throw fail("Run sudah selesai.");
    let source, expected;
    try { source = await getSyncSource(c); expected = expectedFor(source, request); }
    catch (e) { if ([400, 409].includes(e.statusCode)) e.safeToReplan = true; throw e; }
    const resource = request.operation === "HEADER" ? `header:${expected.server}:${expected.institution}` : `device:${p.sid}`;
    const [pending] = await c.query("SELECT id FROM datin_cacti_sync_jobs WHERE run_id = ? AND resource_key = ? AND job_status IN ('PREPARED','ATTEMPTED') FOR UPDATE", [run.id, resource]);
    if (pending.length) throw fail("Resource masih memiliki job belum selesai.");
    const [[master]] = p.sid ? await c.query("SELECT data_json FROM datin_customer_master WHERE spreadsheet_id = ? AND sid = ? FOR UPDATE", [source.spreadsheet_id, p.sid]) : [[]];
    await c.query(`INSERT INTO datin_cacti_sync_jobs (id, run_id, spreadsheet_id, resource_key, sid, operation, request_hash, request_json, expected_json, master_backup_json, source_snapshot_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`, [p.job_id, run.id, source.spreadsheet_id, resource, p.sid || null, p.operation, digest(request), JSON.stringify(request), JSON.stringify(expected), master ? JSON.stringify(parse(master.data_json)) : null, source.snapshot_at]);
    return jobResult({ id: p.job_id, job_status: "PREPARED", expected_json: expected, request_json: request });
  });
}
async function getJob(c, p) {
  await auth(c, p);
  const [[job]] = await c.query("SELECT * FROM datin_cacti_sync_jobs WHERE id = ? AND run_id = ? FOR UPDATE", [p.job_id, p.run_id]);
  if (!job) throw fail("Job tidak dikenali.", 403);
  return job;
}
async function attemptJob(p, database = pool) {
  return tx(database, async c => {
    const job = await getJob(c, p);
    if (job.job_status !== "PREPARED") return { ...jobResult(job), may_write: false };
    const source = await getSyncSource(c), request = parse(job.request_json);
    if (!same(expectedFor(source, request), parse(job.expected_json))) throw fail("Sumber/mapping berubah sebelum write; batalkan job PREPARED.");
    await c.query("UPDATE datin_cacti_sync_jobs SET job_status = 'ATTEMPTED', attempted_at = NOW(3) WHERE id = ?", [job.id]);
    return { success: true, state: "ATTEMPTED", may_write: true };
  });
}
async function cancelJob(p, database = pool) {
  return tx(database, async c => {
    const job = await getJob(c, p);
    if (job.job_status === "CANCELLED") return { success: true };
    if (job.job_status !== "PREPARED" && !(job.job_status === "ATTEMPTED" && p.no_cacti_write === true && p.journal_proof === "NO_MUTATION_INTENT")) throw fail("Write pernah diotorisasi; jangan membatalkan tanpa rekonsiliasi.");
    await c.query("UPDATE datin_cacti_sync_jobs SET job_status = 'CANCELLED', cancelled_at = NOW(3) WHERE id = ?", [job.id]);
    return { success: true };
  });
}

function validateReceipt(job, receipt) {
  const e = parse(job.expected_json), request = parse(job.request_json);
  if (!receipt || typeof receipt !== "object") throw fail("Receipt kosong.", 400);
  if (e.operation === "HEADER") {
    if (!Number.isSafeInteger(receipt.header_id) || receipt.header_id < 1 || receipt.server !== e.server || receipt.tree_id !== e.tree_id
        || receipt.institution !== e.institution || (e.header_id && receipt.header_id !== e.header_id)) throw fail("Receipt header berbeda.");
  } else if (["CREATE", "UPDATE", "VERIFY"].includes(e.operation)) {
    if (!same(receipt.config, e.config) || receipt.server !== e.server || receipt.tree_id !== e.tree_id || receipt.header_id !== e.header_id
        || !Number.isSafeInteger(receipt.host_id) || receipt.host_id < 1 || receipt.exclusive_tree !== true
        || receipt.monitor !== true || receipt.disabled !== false || !receipt.option_guard
        || (e.operation !== "CREATE" && (receipt.host_id !== e.mapping.host_id || receipt.option_guard !== request.observed.option_guard))) throw fail("Receipt device/tree/opsi berbeda dari job.");
  } else if (e.operation === "DELETE") {
    if (receipt.host_id !== e.mapping.host_id || receipt.server !== e.mapping.server || receipt.absent !== true
        || receipt.inventory_complete !== true || receipt.other_host_ids_unchanged !== true || receipt.headers_unchanged !== true || receipt.no_tree_references !== true) throw fail("Ketiadaan host/header belum diverifikasi lengkap.");
  } else if (e.operation === "CLEANUP" && receipt.database_only !== true) throw fail("Receipt cleanup tidak valid.");
  return e;
}
async function archiveNotMaster(c, job) {
  // Truth about a completed Cacti deletion is recorded even if the sheet changed
  // back to OK during the remote call. Purge the master ONLY if NOT is still true.
  let source;
  try { source = await getSyncSource(c); } catch (e) { if (e.statusCode === 409) return; throw e; }
  const rows = source.inventory.rows.filter(r => norm(r.sid) === norm(job.sid));
  if (rows.length !== 1 || rows[0].sid !== job.sid || rows[0].status !== "NOT" || !source.inventory.safe_for_delete) return;
  const [[master]] = await c.query("SELECT data_json, source_status FROM datin_customer_master WHERE spreadsheet_id = ? AND sid = ? FOR UPDATE", [job.spreadsheet_id, job.sid]);
  if (!master || master.source_status !== "NOT") return;
  await c.query("INSERT INTO datin_customer_archives (job_id, spreadsheet_id, sid, reason, data_json, snapshot_at) VALUES (?, ?, ?, 'NOT', ?, ?)", [job.id, job.spreadsheet_id, job.sid, JSON.stringify(parse(master.data_json)), source.snapshot_at]);
  await c.query("DELETE FROM datin_customer_master WHERE spreadsheet_id = ? AND sid = ? AND source_status = 'NOT'", [job.spreadsheet_id, job.sid]);
}
async function completeJob(p, database = pool) {
  return tx(database, async c => {
    const job = await getJob(c, p), e = validateReceipt(job, p.receipt);
    if (job.job_status === "VERIFIED") {
      if (!same(parse(job.receipt_json), p.receipt)) throw fail("Receipt replay berbeda.");
      return { success: true, state: "VERIFIED", replay: true };
    }
    if (job.job_status !== "ATTEMPTED") throw fail("Job belum diotorisasi/terbatalkan.");
    if (e.operation === "HEADER") {
      const [old] = await c.query("SELECT header_id FROM datin_cacti_headers WHERE spreadsheet_id = ? AND server = ? AND tree_id = ? AND normalized_name = ? FOR UPDATE", [job.spreadsheet_id, e.server, e.tree_id, e.institution]);
      if (old.length && old[0].header_id !== p.receipt.header_id) throw fail("Header mapping sudah berubah; tidak ditimpa.");
      if (!old.length) await c.query("INSERT INTO datin_cacti_headers (spreadsheet_id, server, tree_id, institution_name, normalized_name, header_id) VALUES (?, ?, ?, ?, ?, ?)", [job.spreadsheet_id, e.server, e.tree_id, e.institution, e.institution, p.receipt.header_id]);
    } else {
      const [[mapping]] = await c.query("SELECT sid, server, tree_id, host_id, header_id, ownership_status, sync_state FROM datin_cacti_devices WHERE spreadsheet_id = ? AND sid = ? FOR UPDATE", [job.spreadsheet_id, job.sid]);
      if (!same(identity(mapping || null), e.mapping)) throw fail("Mapping berubah sejak job; hasil remote perlu rekonsiliasi.");
      if (["CREATE", "UPDATE", "VERIFY"].includes(e.operation)) {
        if (!mapping) {
          await c.query(`INSERT INTO datin_cacti_devices (spreadsheet_id, sid, server, tree_id, host_id, header_id, ownership_status, verified_at, sync_state, last_applied_json, last_sync_job_id, last_source_snapshot_at)
            VALUES (?, ?, ?, ?, ?, ?, 'VERIFIED', NOW(), 'ACTIVE', ?, ?, ?)`, [job.spreadsheet_id, job.sid, e.server, e.tree_id, p.receipt.host_id, e.header_id, JSON.stringify(e.config), job.id, job.source_snapshot_at]);
        } else {
          await c.query(`UPDATE datin_cacti_devices SET server = ?, tree_id = ?, host_id = ?, header_id = ?, sync_state = 'ACTIVE', ownership_status = 'VERIFIED',
            verified_at = NOW(), deleted_at = NULL, last_applied_json = ?, last_sync_job_id = ?, last_source_snapshot_at = ? WHERE spreadsheet_id = ? AND sid = ?`, [e.server, e.tree_id, p.receipt.host_id, e.header_id, JSON.stringify(e.config), job.id, job.source_snapshot_at, job.spreadsheet_id, job.sid]);
        }
      } else if (e.operation === "DELETE") {
        await c.query("UPDATE datin_cacti_devices SET sync_state = 'DELETED', deleted_at = NOW(3), last_sync_job_id = ?, last_source_snapshot_at = ? WHERE spreadsheet_id = ? AND sid = ?", [job.id, job.source_snapshot_at, job.spreadsheet_id, job.sid]);
      }
      if (["DELETE", "CLEANUP"].includes(e.operation)) await archiveNotMaster(c, job);
    }
    await c.query("UPDATE datin_cacti_sync_jobs SET job_status = 'VERIFIED', receipt_json = ?, verified_at = NOW(3) WHERE id = ?", [JSON.stringify(p.receipt), job.id]);
    return { success: true, state: "VERIFIED", replay: false };
  });
}
async function finishRun(p, database = pool) {
  return tx(database, async c => {
    const run = await auth(c, p);
    const [unfinished] = await c.query("SELECT id FROM datin_cacti_sync_jobs WHERE run_id = ? AND job_status IN ('PREPARED','ATTEMPTED') LIMIT 1", [run.id]);
    if (unfinished.length) throw fail("Ada job belum terverifikasi; simpan run/jurnal untuk resume.");
    await c.query("UPDATE datin_cacti_sync_runs SET run_status = 'DONE', active = NULL, completed_at = NOW(3), summary_json = ? WHERE id = ?", [JSON.stringify(p.summary || {}), run.id]);
    return { success: true, state: "DONE" };
  });
}
async function noteRunError(p, database = pool) {
  return tx(database, async c => {
    const run = await auth(c, p);
    await c.query("UPDATE datin_cacti_sync_runs SET last_error = ?, last_error_at = NOW(3) WHERE id = ?", [String(p.message || "Agent error").slice(0, 512), run.id]);
    return { success: true };
  });
}
module.exports = { enabled, deleteLimit, canonical, desired, statusRow, deletionEligible, identity, expectedFor, validateReceipt, getSyncSource, startRun, prepareJob, attemptJob, cancelJob, completeJob, finishRun, noteRunError };
