const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("crypto");
const { validateBulk, reserveBulk } = require("../src/services/datin-cacti-bulk.service");
const { digest, bindBatchHeader, finishBatch } = require("../src/services/datin-cacti-batch.service");
const { desiredDevice } = require("../src/services/datin-cacti-pilot.service");
const token = "a".repeat(64), now = new Date();
const sheet = process.env.DATIN_SHEET_ID || "16tSELEZFzbH7FdI3NFqnNJx17xVf8GXgvZD5ghfxkLE";
const source = [0, 1].map(i => ({ sid: "00" + i, customer_name: "TEST BANK " + i, customer_detail: "CABANG", status: "OK", server: "SBS", olt_ip: "172.23.225.48", slot: "0", port: "0", onu: "0", serial_number: "SN" }));
function body() {
  const manifest = source.map((r, i) => {
    const { description, ...desired } = desiredDevice(r, r.customer_name);
    return { sid: r.sid, server: r.server, tree_id: 34, institution: r.customer_name, description, desired, creation_profile: {}, header_id: i ? 42 : null };
  });
  return { batch_id: "bulk-12345678-1234-1234-1234-123456789abc", token, spreadsheet_id: sheet, snapshot_at: now, manifest, manifest_sha256: digest(manifest) };
}
function fixture(options = {}) {
  const calls = [], payload = body();
  const connection = {
    beginTransaction: async () => calls.push("begin"), commit: async () => calls.push("commit"), rollback: async () => calls.push("rollback"), release: () => calls.push("release"),
    query: async (sql, args) => {
      calls.push({ sql, args });
      if (options.error && sql.includes(options.error)) throw Object.assign(new Error("duplicate"), { code: "ER_DUP_ENTRY" });
      if (sql.includes("SELECT spreadsheet_id")) return [[{ spreadsheet_id: sheet }]];
      if (sql.includes("SELECT * FROM datin_cacti_batch_runs")) return [options.replay ? [{ spreadsheet_id: sheet, manifest_json: payload.manifest, token_hash: crypto.createHash("sha256").update(options.badToken ? "bad" : token).digest("hex") }] : []];
      if (sql.includes("FROM datin_sheet_sources s")) return [source.map(r => ({ sid: r.sid, data_json: { ...r, ...(options.changed ? { status: "NOT" } : {}) }, age_seconds: 0, last_snapshot_at: now }))];
      if (sql.includes("FROM datin_institution_aliases")) return [[]];
      if (sql.includes("SELECT header_id FROM datin_cacti_headers")) return [options.headerMismatch ? [{ header_id: 99 }] : args[3] === "TEST BANK 1" ? [{ header_id: 42 }] : []];
      if (sql.includes("SELECT host_id FROM datin_cacti_devices")) return [options.mapped ? [{ host_id: 7 }] : []];
      if (sql.includes("SELECT id, server")) return [[{ id: args[1], server: "SBS", header_id: 42 }]];
      if (sql.includes("INSERT")) return [{ affectedRows: 1 }];
      throw new Error("Unexpected SQL: " + sql);
    },
  };
  return { calls, database: { getConnection: async () => connection }, payload };
}

test("bulk validates count/hash/fixed trees/header identities and protects ATCS", () => {
  assert.equal(validateBulk(body()).length, 2);
  for (const mutate of [p => p.manifest.push(p.manifest[0]), p => p.manifest[0].server = "OTHER", p => p.manifest[0].institution = "ATCS", p => p.manifest[0].tree_id = 14, p => p.manifest[0].header_id = 0, p => p.manifest[0].desired.slot = "9", p => p.token = "secret", p => p.manifest = [], p => p.manifest = Array(1001).fill(p.manifest[0])]) {
    const p = body(); mutate(p); assert.throws(() => validateBulk(p));
  }
});
test("bulk reserves all SIDs atomically with only one new-header lease and hashed tokens", async () => {
  const f = fixture(), result = await reserveBulk(f.payload, f.database);
  assert.equal(result.jobs.length, 2);
  assert.equal(f.calls.filter(c => c.sql?.includes("INSERT INTO datin_cacti_batch_headers")).length, 1);
  assert.equal(f.calls.filter(c => c.sql?.includes("INSERT INTO datin_cacti_pilot_jobs")).length, 2);
  assert.ok(f.calls.includes("commit"));
  const jobInsert = f.calls.find(c => c.sql?.includes("INSERT INTO datin_cacti_pilot_jobs"));
  assert.notEqual(jobInsert.args[6], result.jobs[0].token);
  assert.notEqual(result.jobs[0].token, result.jobs[1].token);
  assert.equal(jobInsert.args[6], crypto.createHash("sha256").update(result.jobs[0].token).digest("hex"));
});
test("lost reservation response replays identical job tokens without more inserts", async () => {
  const fresh = fixture(), first = await reserveBulk(fresh.payload, fresh.database);
  const replay = fixture({ replay: true }), second = await reserveBulk(replay.payload, replay.database);
  assert.deepEqual(first.jobs, second.jobs);
  assert.ok(!replay.calls.some(c => c.sql?.includes("INSERT")));
  const bad = fixture({ replay: true, badToken: true });
  await assert.rejects(reserveBulk(bad.payload, bad.database));
});
test("changed data, mappings, header identities and concurrent reservations roll back", async () => {
  for (const options of [{ changed: true }, { mapped: true }, { headerMismatch: true }, { error: "INSERT INTO datin_cacti_pilot_jobs" }]) {
    const f = fixture(options);
    await assert.rejects(reserveBulk(f.payload, f.database));
    assert.ok(f.calls.includes("rollback")); assert.ok(!f.calls.includes("commit"));
  }
});
test("bulk receipt authentication supports header replay and completion counts actual manifest", async () => {
  const p = body(), stored = { spreadsheet_id: sheet, token_hash: crypto.createHash("sha256").update(token).digest("hex"), manifest_json: p.manifest };
  const conn = { beginTransaction: async () => {}, commit: async () => {}, rollback: async () => {}, release() {}, query: async sql => {
    if (sql.includes("SELECT * FROM datin_cacti_batch_runs")) return [[stored]];
    if (sql.includes("SELECT * FROM datin_cacti_batch_headers")) return [[{ header_status: "VERIFIED", header_id: 77 }]];
    if (sql.includes("SELECT d.host_id")) return [[{ host_id: 5 }]];
    return [{ affectedRows: 1 }];
  } };
  const db = { getConnection: async () => conn };
  assert.equal((await bindBatchHeader({ ...p, server: "SBS", tree_id: 34, institution: "TEST BANK 0", header_id: 77 }, db)).replay, true);
  assert.equal((await finishBatch(p, db)).devices, 2);
  await assert.rejects(finishBatch({ ...p, token: "b".repeat(64) }, db));
});
