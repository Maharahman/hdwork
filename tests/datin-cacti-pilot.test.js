const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("crypto");
const { reservePilot, completePilot, desiredDevice, pilotCapabilities } = require("../src/services/datin-cacti-pilot.service");

const source = { sid: "1165651712", server: "SBS", status: "OK", customer_name: "BANK JATIM", customer_detail: "KF KENJERAN", olt_ip: "172.23.225.42", slot: "8", port: "6", onu: "19", serial_number: "ZTEGCC82D5F7" };
const now = new Date();
const sheet = "16tSELEZFzbH7FdI3NFqnNJx17xVf8GXgvZD5ghfxkLE";
const payload = () => ({ spreadsheet_id: sheet, snapshot_at: now, sid: source.sid, server: "SBS", tree_id: 34, header_id: 9127, desired: desiredDevice(source) });

function dbFixture({ existing = false, duplicate = false, job = null } = {}) {
  const calls = [];
  const connection = {
    beginTransaction: async () => calls.push("begin"), commit: async () => calls.push("commit"),
    rollback: async () => calls.push("rollback"), release: () => calls.push("release"),
    query: async (sql, args) => {
      calls.push({ sql, args });
      if (sql.includes("JOIN datin_customer_master")) return [[{ data_json: source, last_snapshot_at: now }]];
      if (sql.includes("SELECT host_id FROM datin_cacti_devices")) return [existing ? [{ host_id: 42 }] : []];
      if (sql.includes("SELECT * FROM datin_cacti_pilot_jobs")) return [job ? [job] : []];
      if (duplicate && sql.includes("INSERT INTO datin_cacti_pilot_jobs")) throw Object.assign(new Error("duplicate"), { code: "ER_DUP_ENTRY" });
      return [{}];
    },
  };
  const db = { getConnection: async () => connection, query: async (sql) => {
    if (sql.includes("FROM datin_institution_aliases")) return [[]];
    return [[{ last_snapshot_at: now, age_seconds: 0, sid: source.sid, data_json: source }]];
  } };
  return { db, calls };
}

test("pilot scope is two fixed targets and zero remains a valid value", () => {
  assert.equal(pilotCapabilities().bulk_enabled, false);
  assert.equal(Object.keys(pilotCapabilities().targets).length, 2);
  assert.equal(desiredDevice({ ...source, slot: "0", port: "0", onu: "0" }).slot, "0");
  for (const row of [{ ...source, slot: null }, { ...source, status: "NOT" }, { ...source, olt_ip: "bad" }, { ...source, onu: "1000" }]) assert.throws(() => desiredDevice(row));
});

test("reservation validates exact pilot/snapshot/config and commits only a hashed token", async () => {
  const { db, calls } = dbFixture();
  const result = await reservePilot(payload(), db);
  assert.match(result.token, /^[0-9a-f]{64}$/);
  const insert = calls.find((c) => c.sql?.includes("INSERT INTO datin_cacti_pilot_jobs"));
  assert.notEqual(insert.args[6], result.token);
  assert.equal(insert.args[6], crypto.createHash("sha256").update(result.token).digest("hex"));
  assert.ok(calls.includes("commit"));
  for (const p of [{ ...payload(), sid: "another" }, { ...payload(), header_id: 9218 }, { ...payload(), snapshot_at: "2000-01-01" }, { ...payload(), desired: { ...payload().desired, slot: "9" } }]) await assert.rejects(reservePilot(p, db));
});

test("existing mapping and concurrent duplicate reservation fail closed without expiry/retry", async () => {
  for (const options of [{ existing: true }, { duplicate: true }]) {
    const { db, calls } = dbFixture(options);
    await assert.rejects(reservePilot(payload(), db), (e) => e.statusCode === 409);
    assert.ok(calls.includes("rollback"));
    assert.ok(calls.includes("release"));
    assert.ok(!calls.some((c) => c.sql?.includes("INSERT INTO datin_cacti_devices")));
  }
});

function completion(state = "RESERVED") {
  const token = "a".repeat(64), id = "12345678-1234-1234-1234-123456789012";
  const job = { id, spreadsheet_id: sheet, sid: source.sid, server: "SBS", tree_id: 34, header_id: 9127, token_hash: crypto.createHash("sha256").update(token).digest("hex"), desired_json: desiredDevice(source), job_status: state, host_id: state === "VERIFIED" ? 42 : null };
  const receipt = { ...desiredDevice(source), server: "SBS", tree_id: 34, header_id: 9127, host_id: 42, monitor: true, disabled: false, host_template_id: "0", snmp_version: "0", availability_method: "3", ping_method: "1" };
  return { job, body: { job_id: id, token, host_id: 42, receipt } };
}

test("completion stores VERIFIED mapping and marks job in one transaction", async () => {
  const { job, body } = completion(), { db, calls } = dbFixture({ job });
  assert.equal((await completePilot(body, db)).state, "VERIFIED");
  assert.ok(calls.some((c) => c.sql?.includes("INSERT INTO datin_cacti_devices")));
  assert.ok(calls.some((c) => c.sql?.includes("UPDATE datin_cacti_pilot_jobs")));
  assert.ok(calls.includes("commit"));
});

test("wrong token or receipt cannot save mapping", async () => {
  const { job, body } = completion();
  for (const change of [{ token: "b".repeat(64) }, { receipt: { ...body.receipt, monitor: false } }, { receipt: { ...body.receipt, header_id: 9218 } }, { receipt: { ...body.receipt, slot: "0" } }]) {
    const { db, calls } = dbFixture({ job });
    await assert.rejects(completePilot({ ...body, ...change }, db));
    assert.ok(calls.includes("rollback"));
    assert.ok(!calls.some((c) => c.sql?.includes("INSERT INTO datin_cacti_devices")));
  }
});

test("completion replay is idempotent and never overwrites another host", async () => {
  const { job, body } = completion("VERIFIED"), { db, calls } = dbFixture({ job });
  assert.equal((await completePilot(body, db)).replay, true);
  assert.ok(!calls.some((c) => c.sql?.includes("INSERT INTO datin_cacti_devices")));
  await assert.rejects(completePilot({ ...body, host_id: 43, receipt: { ...body.receipt, host_id: 43 } }, db), (e) => e.statusCode === 409);
});

test("device completion cannot run before the batch header is bound", async () => {
  const { job, body } = completion();
  job.header_id = 0;
  const { db, calls } = dbFixture({ job });
  await assert.rejects(completePilot({ ...body, receipt: { ...body.receipt, header_id: 0 } }, db), (e) => e.statusCode === 409);
  assert.ok(!calls.some((c) => c.sql?.includes("INSERT INTO datin_cacti_devices")));
});
