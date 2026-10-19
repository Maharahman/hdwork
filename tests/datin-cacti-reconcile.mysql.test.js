// Optional real-MySQL test. Uses ONLY an explicitly supplied Unix socket and
// a newly created synthetic schema; never loads the user's .env database.
const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const mysql = require("mysql2/promise");
const { syncSnapshot } = require("../src/services/datin-sheet-sync.service");
const svc = require("../src/services/datin-cacti-reconcile.service");

test("real MySQL migrations and OK → CREATE → UPDATE → NOT/archive/delete → OK/recreate", { skip: !process.env.HDWORK_RECONCILE_MYSQL_SOCKET }, async t => {
  const socketPath = process.env.HDWORK_RECONCILE_MYSQL_SOCKET;
  assert.ok(socketPath.startsWith("/tmp/hdwork-reconcile-mysql."), "Only an isolated temporary MySQL socket is allowed");
  const admin = await mysql.createConnection({ socketPath, user: "root", multipleStatements: true });
  const schema = "hdwork_test_reconcile_" + crypto.randomBytes(6).toString("hex");
  let db;
  const oldEnabled = process.env.DATIN_CACTI_AUTOMATION_ENABLED;
  process.env.DATIN_CACTI_AUTOMATION_ENABLED = "1";
  t.after(async () => {
    if (oldEnabled === undefined) delete process.env.DATIN_CACTI_AUTOMATION_ENABLED; else process.env.DATIN_CACTI_AUTOMATION_ENABLED = oldEnabled;
    if (db) await db.end();
    assert.match(schema, /^hdwork_test_reconcile_[0-9a-f]{12}$/);
    await admin.query(`DROP DATABASE IF EXISTS \`${schema}\``);
    await admin.end();
  });
  await admin.query(`CREATE DATABASE \`${schema}\` CHARACTER SET utf8mb4`);
  db = mysql.createPool({ socketPath, user: "root", database: schema, multipleStatements: true, connectionLimit: 4 });
  for (const file of ["20260928_add_datin_personal_subscriptions.sql", "20261018_add_datin_sheet_sync.sql", "20261018_add_datin_cacti_headers.sql", "20261019_add_datin_cacti_devices.sql", "20261019_add_datin_cacti_pilot_jobs.sql", "20261019_add_datin_cacti_batch.sql", "20261019_add_datin_cacti_reconcile.sql"]) {
    await db.query(fs.readFileSync(path.join(__dirname, "../database/migrations", file), "utf8"));
  }
  const sheet = process.env.DATIN_SHEET_ID || "16tSELEZFzbH7FdI3NFqnNJx17xVf8GXgvZD5ghfxkLE";
  const row = { sid: "0001", source_row: 2, status: "OK", server: "SBS", customer_name: "BANK JATIM", customer_detail: "TEST", slot: "0", port: "0", onu: "0", olt_ip: "172.23.225.42", serial_number: "TESTSN" };
  let tick = Date.now() - 20000;
  async function sync(r = row, oldProtocol = false) {
    const all = [r, { ...row, sid: "0002", source_row: 3 }];
    const p = { spreadsheet_id: sheet, sheet_name: "Sheet1", run_id: crypto.randomUUID(), snapshot_at: new Date(tick += 1000).toISOString(), rows: all.filter(r => r.status === "OK") };
    if (!oldProtocol) {
      p.institution_inventory = { version: 1, complete: true, total_rows: 2, blank_rows: 0, rows: all.map(({ source_row, customer_name, status }) => ({ source_row, customer_name, status })) };
      p.device_inventory = { version: 1, complete: true, total_rows: 2, blank_rows: 0, rows: all.map(({ source_row, sid, server, customer_name, status }) => ({ source_row, sid, server, customer_name, status })) };
    }
    return syncSnapshot(p, db);
  }
  await sync();
  const run = { run_id: crypto.randomUUID(), token: "a".repeat(64) };
  await svc.startRun({ ...run, spreadsheet_id: sheet }, db);
  await svc.noteRunError({ ...run, message: "synthetic recoverable timeout" }, db);
  const [[diagnostic]] = await db.query("SELECT last_error FROM datin_cacti_sync_runs WHERE id = ?", [run.run_id]);
  assert.equal(diagnostic.last_error, "synthetic recoverable timeout");
  await assert.rejects(svc.startRun({ run_id: crypto.randomUUID(), token: "b".repeat(64), spreadsheet_id: sheet }, db), e => e.statusCode === 409);
  async function job(p, receipt) {
    const body = { ...run, job_id: crypto.randomUUID(), ...p };
    await svc.prepareJob(body, db);
    assert.equal((await svc.attemptJob(body, db)).may_write, true);
    assert.equal((await svc.attemptJob(body, db)).may_write, false);
    await svc.completeJob({ ...body, receipt }, db);
    assert.equal((await svc.completeJob({ ...body, receipt }, db)).replay, true);
    return body;
  }
  await job({ operation: "HEADER", observed: { server: "SBS", tree_id: 34, institution: "BANK JATIM", header_id: 9127 } },
    { server: "SBS", tree_id: 34, institution: "BANK JATIM", header_id: 9127 });
  let source = await svc.getSyncSource(db);
  const receipt = (config, host_id = 1001) => ({ config, host_id, server: "SBS", tree_id: 34, header_id: 9127, monitor: true, disabled: false, exclusive_tree: true, option_guard: "guard" });
  await job({ operation: "CREATE", sid: "0001", desired: svc.desired(source, "0001").config, observed: { absent: true, inventory_complete: true, header_id: 9127 } }, receipt(svc.desired(source, "0001").config));
  source = await svc.getSyncSource(db);
  assert.equal(source.mappings[0].host_id, 1001);
  await sync({ ...row, slot: "9" });
  source = await svc.getSyncSource(db);
  await job({ operation: "UPDATE", sid: "0001", desired: svc.desired(source, "0001").config, observed: { mapping: svc.identity(source.mappings[0]), config: { before: true }, option_guard: "guard", header_id: 9127 } }, receipt(svc.desired(source, "0001").config));
  const [[updated]] = await db.query("SELECT host_id, last_applied_json, last_source_snapshot_at FROM datin_cacti_devices WHERE sid = '0001'");
  assert.equal(updated.host_id, 1001); assert.equal((typeof updated.last_applied_json === "string" ? JSON.parse(updated.last_applied_json) : updated.last_applied_json).slot, "9");
  assert.ok(updated.last_source_snapshot_at);
  await sync({ ...row, status: "NOT" });
  source = await svc.getSyncSource(db);
  const [[pending]] = await db.query("SELECT source_status FROM datin_customer_master WHERE sid = '0001'");
  assert.equal(pending.source_status, "NOT");
  await job({ operation: "DELETE", sid: "0001", observed: { mapping: svc.identity(source.mappings[0]), absent: true, inventory_complete: true } },
    { server: "SBS", host_id: 1001, absent: true, inventory_complete: true, other_host_ids_unchanged: true, headers_unchanged: true, no_tree_references: true });
  const [remaining] = await db.query("SELECT sid FROM datin_customer_master ORDER BY sid");
  assert.deepEqual(remaining.map(r => r.sid), ["0002"]);
  const [[archive]] = await db.query("SELECT COUNT(*) AS n FROM datin_customer_archives"); assert.equal(archive.n, 1);
  const [[deleted]] = await db.query("SELECT sync_state FROM datin_cacti_devices WHERE sid = '0001'"); assert.equal(deleted.sync_state, "DELETED");
  await sync(row);
  source = await svc.getSyncSource(db);
  await job({ operation: "CREATE", sid: "0001", desired: svc.desired(source, "0001").config, observed: { absent: true, inventory_complete: true, header_id: 9127 } }, receipt(svc.desired(source, "0001").config, 1002));
  const [[recreated]] = await db.query("SELECT host_id, sync_state FROM datin_cacti_devices WHERE sid = '0001'");
  assert.equal(recreated.host_id, 1002); assert.equal(recreated.sync_state, "ACTIVE");
  source = await svc.getSyncSource(db);
  const cancellable = { ...run, job_id: crypto.randomUUID(), sid: "0001", operation: "VERIFY", desired: svc.desired(source, "0001").config,
    observed: { mapping: svc.identity(source.mappings[0]), config: svc.desired(source, "0001").config, option_guard: "guard", header_id: 9127 } };
  await svc.prepareJob(cancellable, db); await svc.attemptJob(cancellable, db);
  await svc.cancelJob({ ...cancellable, no_cacti_write: true, journal_proof: "NO_MUTATION_INTENT" }, db);
  const [[cancelled]] = await db.query("SELECT job_status, cancelled_at FROM datin_cacti_sync_jobs WHERE id = ?", [cancellable.job_id]);
  assert.equal(cancelled.job_status, "CANCELLED"); assert.ok(cancelled.cancelled_at);
  await svc.finishRun(run, db);
  const [[finished]] = await db.query("SELECT run_status, active FROM datin_cacti_sync_runs WHERE id = ?", [run.run_id]);
  assert.equal(finished.run_status, "DONE"); assert.equal(finished.active, null);
  await sync(row, true);
  await assert.rejects(svc.getSyncSource(db), e => e.statusCode === 409);
});
