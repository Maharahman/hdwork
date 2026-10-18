const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("crypto");
const config = require("../src/config/datin-cacti-batch.json");
const service = require("../src/services/datin-cacti-batch.service");
const { desiredDevice } = require("../src/services/datin-cacti-pilot.service");
const now = new Date(), token = "a".repeat(64);
const source = config.targets.map((target) => ({ ...target, customer_name: target.institution, customer_detail: "TEST", status: "OK", olt_ip: "172.23.225.48", slot: "0", port: "0", onu: "0", serial_number: "TESTSN" }));
const manifest = source.map((row) => ({ sid: row.sid, server: row.server, tree_id: row.tree_id, institution: row.institution,
  description: desiredDevice(row, row.institution).description, desired: Object.fromEntries(["olt_ip", "slot", "port", "onu", "serial_number"].map((key) => [key, row[key]])), creation_profile: { monitor: true } }));
const settings = { ...config, approved_sha256: service.digest(manifest) };

function fixture(options = {}) {
  const calls = [];
  const sheet = process.env.DATIN_SHEET_ID || "16tSELEZFzbH7FdI3NFqnNJx17xVf8GXgvZD5ghfxkLE";
  const batch = { batch_id: config.batch_id, spreadsheet_id: sheet, token_hash: crypto.createHash("sha256").update(token).digest("hex"), manifest_json: manifest };
  const connection = {
    beginTransaction: async () => calls.push("begin"), commit: async () => calls.push("commit"), rollback: async () => calls.push("rollback"), release: () => calls.push("release"),
    query: async (sql, args) => {
      calls.push({ sql, args });
      if (options.error && sql.includes(options.error.on)) throw Object.assign(new Error("failure"), { code: options.error.code });
      if (sql.includes("SELECT m.data_json")) return [[{ last_snapshot_at: now, data_json: { ...source.find((row) => row.sid === args[1]), ...(options.changed ? { slot: "9" } : {}) } }]];
      if (sql.includes("SELECT host_id FROM datin_cacti_devices")) return [options.existing ? [{ host_id: 42 }] : []];
      if (sql.includes("SELECT header_id FROM datin_cacti_headers")) return [options.oldHeader ? [{ header_id: 99 }] : []];
      if (sql.includes("SELECT * FROM datin_cacti_batch_runs")) return [[batch]];
      if (sql.includes("SELECT * FROM datin_cacti_batch_headers")) return [[{ header_status: options.replay ? "VERIFIED" : "RESERVED", header_id: 99 }]];
      if (sql.includes("SELECT d.host_id")) return [options.incomplete ? [] : [{ host_id: 42 }]];
      return [{ affectedRows: options.zeroUpdate ? 0 : 1 }];
    },
  };
  const database = { getConnection: async () => connection, query: async (sql) => {
    if (sql.includes("FROM datin_institution_aliases")) return [[]];
    return [source.map((row) => ({ sid: row.sid, data_json: row, last_snapshot_at: now, age_seconds: 0 }))];
  } };
  return { database, calls, sheet };
}

test("batch manifest binds 10 exact targets and complete configuration", () => {
  assert.equal(service.validateManifest(manifest, settings).length, 10);
  assert.equal(service.batchCapabilities().unrestricted_bulk, false);
  for (const rows of [manifest.slice(1), [...manifest.slice(0, 9), manifest[0]], [null, ...manifest.slice(1)], manifest.map((row, i) => i ? row : { ...row, server: "SBU" }), manifest.map((row, i) => i ? row : { ...row, desired: { ...row.desired, slot: "9" } })]) assert.throws(() => service.validateManifest(rows, settings));
  // The production hash does not approve test fixtures.
  assert.throws(() => service.validateManifest(manifest));
});

test("batch reservation atomically creates 4 header leases and 10 SID jobs", async () => {
  const { database, calls, sheet } = fixture();
  const result = await service.reserveBatch({ batch_id: config.batch_id, spreadsheet_id: sheet, snapshot_at: now, manifest }, database, settings);
  assert.equal(result.jobs.length, 10);
  assert.equal(result.headers.length, 4);
  assert.equal(calls.filter((item) => item.sql?.includes("INSERT INTO datin_cacti_pilot_jobs")).length, 10);
  assert.equal(calls.filter((item) => item.sql?.includes("INSERT INTO datin_cacti_batch_headers")).length, 4);
  const insertion = calls.find((item) => item.sql?.includes("INSERT INTO datin_cacti_batch_runs"));
  assert.notEqual(insertion.args[2], result.token);
  assert.ok(calls.includes("commit"));
});

test("duplicate reservations, existing mappings, changed source and missing migration roll back", async () => {
  for (const options of [{ existing: true }, { oldHeader: true }, { changed: true }, { error: { on: "INSERT INTO datin_cacti_batch_runs", code: "ER_DUP_ENTRY" } }, { error: { on: "INSERT INTO datin_cacti_batch_runs", code: "ER_NO_SUCH_TABLE" } }]) {
    const { database, calls, sheet } = fixture(options);
    await assert.rejects(service.reserveBatch({ batch_id: config.batch_id, spreadsheet_id: sheet, snapshot_at: now, manifest }, database, settings), (error) => error.statusCode === 409 || error.statusCode === 503);
    assert.ok(calls.includes("rollback"));
    assert.ok(!calls.includes("commit"));
  }
});

function headerBody() {
  return { batch_id: config.batch_id, token, server: "SBS", tree_id: 34, institution: "BANK DANAMON", header_id: 99 };
}

test("verified header binds the same new ID to all its device jobs without overwriting old mappings", async () => {
  const { database, calls } = fixture();
  const result = await service.bindBatchHeader(headerBody(), database);
  assert.equal(result.header_id, 99);
  assert.equal(calls.filter((item) => item.sql?.includes("UPDATE datin_cacti_pilot_jobs")).length, 3);
  assert.equal(calls.filter((item) => item.sql?.includes("INSERT INTO datin_cacti_headers")).length, 1);
  assert.ok(!calls.some((item) => item.sql?.includes("ON DUPLICATE")));
});

test("foreign header/token and concurrent job changes cannot bind headers", async () => {
  for (const changes of [{ institution: "ATCS" }, { token: "b".repeat(64) }, { tree_id: 28 }, { header_id: 0 }]) {
    const { database } = fixture();
    await assert.rejects(service.bindBatchHeader({ ...headerBody(), ...changes }, database));
  }
  const { database, calls } = fixture({ zeroUpdate: true });
  await assert.rejects(service.bindBatchHeader(headerBody(), database));
  assert.ok(calls.includes("rollback"));
});

test("header replay is idempotent and batch finishes only after all mappings are verified", async () => {
  const replay = fixture({ replay: true });
  assert.equal((await service.bindBatchHeader(headerBody(), replay.database)).replay, true);
  assert.ok(!replay.calls.some((item) => item.sql?.includes("INSERT")));
  await assert.rejects(service.bindBatchHeader({ ...headerBody(), header_id: 100 }, replay.database));
  const incomplete = fixture({ incomplete: true });
  await assert.rejects(service.finishBatch({ batch_id: config.batch_id, token }, incomplete.database), (e) => e.statusCode === 409);
  const complete = fixture();
  assert.equal((await service.finishBatch({ batch_id: config.batch_id, token }, complete.database)).devices, 10);
});
