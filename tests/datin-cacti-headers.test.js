const test = require("node:test");
const assert = require("node:assert/strict");
const express = require("express");
const { groupInstitutions, getHeaderPlan, getDeviceSource, saveHeaderMappings, getDeviceMappings } = require("../src/services/datin-cacti-headers.service");

test("header plans group institution per server and exclude invalid server/status", () => {
  const result = groupInstitutions([
    { sid: "a", data_json: { server: "sbs", customer_name: " Bank   Jatim ", status: "OK" } },
    { sid: "b", data_json: { server: "SBS", customer_name: "BANK JATIM", status: "OK" } },
    { sid: "c", data_json: { server: "SBU", customer_name: "BANK JATIM", status: "OK" } },
    { sid: "d", data_json: { server: "NOT FOUND", customer_name: "SKIP", status: "OK" } },
    { sid: "e", data_json: { server: "SBS", customer_name: "SKIP", status: "NOT" } },
  ]);
  assert.equal(result.institutions.length, 2);
  assert.equal(result.institutions[0].customer_count, 2);
  assert.equal(result.institutions[0].tree_id, 34);
  assert.equal(result.institutions[1].tree_id, 28);
  assert.equal(result.skipped.length, 2);
});

test("plan uses only latest accepted snapshot and rejects absent or stale sources", async () => {
  let queryText;
  const db = { query: async (sql) => { queryText = sql; return [[{ last_snapshot_at: new Date(), age_seconds: 10, sid: null }]]; } };
  const result = await getHeaderPlan(db);
  assert.match(queryText, /m.valid_snapshot_at = s.last_snapshot_at/);
  assert.deepEqual(result.institutions, []);
  await assert.rejects(getHeaderPlan({ query: async () => [[]] }), (error) => error.statusCode === 409);
  await assert.rejects(getHeaderPlan({ query: async () => [[{ last_snapshot_at: new Date(), age_seconds: 99999 }]] }), (error) => error.statusCode === 409);
});

test("mapping rejects wrong tree, unknown institution and changed snapshot", async () => {
  const now = new Date();
  const db = { query: async () => [[{ last_snapshot_at: now, age_seconds: 0, sid: "a", data_json: { server: "SBS", customer_name: "BANK JATIM", status: "OK" } }]] };
  const plan = await getHeaderPlan(db);
  const payload = { spreadsheet_id: plan.spreadsheet_id, snapshot_at: now, mappings: [{ server: "SBS", name: "BANK JATIM", tree_id: 28, header_id: 1 }] };
  await assert.rejects(saveHeaderMappings(payload, db), /Mapping/);
  await assert.rejects(saveHeaderMappings({ ...payload, mappings: [{ ...payload.mappings[0], tree_id: 34, name: "OTHER" }] }, db), /Mapping/);
  await assert.rejects(saveHeaderMappings({ ...payload, snapshot_at: "2020-01-01" }, db), (error) => error.statusCode === 409);
});

test("comparison source exposes current records, conflict rows and explicit institution aliases", async () => {
  let sourceQuery;
  const database = { query: async (sql) => {
    if (sql.includes("FROM datin_institution_aliases")) return [[{ normalized_name: "BPK", institution_name: "BADAN PEMERIKSA KEUANGAN" }]];
    sourceQuery = sql;
    return [[{ last_snapshot_at: new Date(), age_seconds: 1, sid: "000123", source_row: 4, data_json: JSON.stringify({ sid: "000123", server: "SBS", status: "OK", slot: "3" }), result_json: JSON.stringify({ summary: { conflicts: [{ sid: "duplicate", rows: [2, 3] }] } }) }]];
  } };
  const result = await getDeviceSource(database);
  assert.match(sourceQuery, /m.valid_snapshot_at = s.last_snapshot_at/);
  assert.equal(result.customers[0].sid, "000123");
  assert.equal(result.customers[0].source_row, 4);
  assert.deepEqual(result.conflicts, [{ sid: "duplicate", rows: [2, 3] }]);
  assert.deepEqual(result.aliases, [{ alias: "BPK", name: "BADAN PEMERIKSA KEUANGAN" }]);
});

test("verified mappings are saved transactionally and failures roll back", async () => {
  const now = new Date();
  const calls = [];
  const connection = {
    beginTransaction: async () => calls.push("begin"),
    commit: async () => calls.push("commit"), rollback: async () => calls.push("rollback"), release: () => calls.push("release"),
    query: async (sql, args) => { calls.push(args); },
  };
  const db = {
    query: async () => [[{ last_snapshot_at: now, age_seconds: 0, sid: "a", data_json: { server: "SBS", customer_name: "BANK JATIM", status: "OK" } }]],
    getConnection: async () => connection,
  };
  const plan = await getHeaderPlan(db);
  const payload = { spreadsheet_id: plan.spreadsheet_id, snapshot_at: now, mappings: [{ server: "SBS", name: "BANK JATIM", tree_id: 34, header_id: 9127 }] };
  assert.equal((await saveHeaderMappings(payload, db)).saved, 1);
  assert.ok(calls.includes("commit"));
  assert.equal(calls.find(Array.isArray)[5], 9127);
  calls.length = 0;
  connection.query = async () => { throw new Error("database unavailable"); };
  await assert.rejects(saveHeaderMappings(payload, db), /database unavailable/);
  assert.ok(calls.includes("rollback"));
  assert.ok(calls.includes("release"));
});

test("Cacti agent endpoints reject the spreadsheet key and require their own key", async (t) => {
  const oldKey = process.env.DATIN_CACTI_AGENT_API_KEY;
  process.env.DATIN_CACTI_AGENT_API_KEY = "agent-test-key";
  const app = express();
  app.use(express.json());
  app.use("/api/datin-cacti", require("../src/routes/datin-cacti.routes"));
  const server = app.listen(0, "127.0.0.1");
  await new Promise((resolve) => server.once("listening", resolve));
  t.after(async () => {
    await new Promise((resolve) => server.close(resolve));
    if (oldKey === undefined) delete process.env.DATIN_CACTI_AGENT_API_KEY;
    else process.env.DATIN_CACTI_AGENT_API_KEY = oldKey;
  });
  const url = `http://127.0.0.1:${server.address().port}/api/datin-cacti/header-plan`;
  assert.equal((await fetch(url)).status, 401);
  assert.equal((await fetch(url, { headers: { "X-Sheet-Sync-Key": "sheet-key" } })).status, 401);
  assert.equal((await fetch(url, { headers: { "X-Cacti-Agent-Key": "wrong-key" } })).status, 403);
  const mappingsUrl = url.replace("header-plan", "device-mappings");
  assert.equal((await fetch(mappingsUrl)).status, 401);
  assert.equal((await fetch(mappingsUrl, { headers: { "X-Cacti-Agent-Key": "wrong-key" } })).status, 403);
  assert.equal((await fetch(mappingsUrl, { method: "POST", headers: { "X-Cacti-Agent-Key": "agent-test-key" } })).status, 404);
  for (const endpoint of ["batch-capabilities", "batch-reservations", "batch-headers", "batch-completions"]) {
    const batchUrl = url.replace("header-plan", endpoint);
    const method = endpoint === "batch-capabilities" ? "GET" : "POST";
    assert.equal((await fetch(batchUrl, { method })).status, 401);
    assert.equal((await fetch(batchUrl, { method, headers: { "X-Cacti-Agent-Key": "wrong" } })).status, 403);
  }
});

test("device mappings are scoped to the source and read only; missing migration fails closed", async () => {
  const now = new Date();
  let argsSeen;
  const db = { query: async (sql, args) => {
    if (sql.includes("FROM datin_cacti_devices")) {
      assert.match(sql, /WHERE spreadsheet_id = \?/);
      assert.doesNotMatch(sql, /INSERT|UPDATE|DELETE/i);
      argsSeen = args;
      return [[{ sid: "000123", server: "SBS", host_id: 42, ownership_status: "REVIEW_REQUIRED" }]];
    }
    return [[{ last_snapshot_at: now, age_seconds: 1, sid: null }]];
  } };
  const result = await getDeviceMappings(db);
  assert.deepEqual(argsSeen, [result.spreadsheet_id]);
  assert.equal(result.mappings[0].sid, "000123");
  assert.equal(result.snapshot_at, now);
  const missing = { query: async (sql) => {
    if (sql.includes("FROM datin_cacti_devices")) throw Object.assign(new Error("missing"), { code: "ER_NO_SUCH_TABLE" });
    return [[{ last_snapshot_at: now, age_seconds: 1 }]];
  } };
  await assert.rejects(getDeviceMappings(missing), (error) => error.statusCode === 503 && /20261019/.test(error.message));
});
