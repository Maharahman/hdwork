const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const express = require("express");
const { prepareSnapshot, syncSnapshot } = require("../src/services/datin-sheet-sync.service");

function snapshot(rows) {
  return {
    spreadsheet_id: "16tSELEZFzbH7FdI3NFqnNJx17xVf8GXgvZD5ghfxkLE", sheet_name: "Sheet1",
    run_id: "ef67c192-4af4-4598-a765-f4f1fc07c410", snapshot_at: new Date().toISOString(), rows,
  };
}

function row(sid, source_row, status = "OK", name = "BANK JATIM") {
  return { sid, source_row, status, customer_name: name };
}

test("only unique OK SIDs are accepted; duplicates and blanks cannot overwrite master", () => {
  const result = prepareSnapshot(snapshot([
    row("000123", 2), row("same", 3), row("same", 4, "OK", ""),
    row("000123", 5, "NOT"), row("", 6), row("unique", 7, " ok "),
  ]));
  assert.deepEqual(result.records.map((record) => record.data.sid), ["000123", "unique"]);
  assert.deepEqual(result.summary.conflicts, [{ sid: "same", rows: [3, 4] }]);
  assert.equal(result.summary.ignored, 1);
  assert.equal(result.summary.invalid_rows.length, 1);
  assert.equal(result.records[1].data.status, "OK");
  assert.throws(() => prepareSnapshot(snapshot([row(123, 2)])), /harus berupa teks/);
  assert.throws(() => prepareSnapshot({ ...snapshot([]), sheet_name: "Copy of Sheet1" }), /Sumber/);
});

function database(options = {}) {
  const calls = [];
  const connection = {
    beginTransaction: async () => calls.push("begin"),
    commit: async () => calls.push("commit"),
    rollback: async () => calls.push("rollback"),
    release: () => calls.push("release"),
    query: async (sql, args) => {
      calls.push({ sql, args });
      if (sql.includes("FOR UPDATE")) return [[{ last_snapshot_at: options.lastSnapshot || null }]];
      if (sql.startsWith("SELECT result_json")) return [options.previous ? [{ result_json: options.previous }] : []];
      if (sql.startsWith("SELECT sid")) return [options.existing || []];
      if (options.failWrite && sql.includes("INSERT INTO datin_customer_master")) throw new Error("write failed");
      return [{}];
    },
  };
  return { calls, getConnection: async () => connection };
}

test("snapshot counts new, updated and unchanged records and keeps skipped SIDs intact", async () => {
  const payload = snapshot([row("new", 2), row("changed", 3), row("same", 4), row("held", 5), row("held", 6)]);
  const sameHash = prepareSnapshot(payload).records.find((record) => record.data.sid === "same").hash;
  const db = database({ existing: [{ sid: "same", data_hash: sameHash }, { sid: "changed", data_hash: "old" }] });
  const result = await syncSnapshot(payload, db);
  assert.equal(result.summary.created, 1);
  assert.equal(result.summary.updated, 1);
  assert.equal(result.summary.unchanged, 1);
  const writes = db.calls.filter((call) => call.sql?.includes("INSERT INTO datin_customer_master"));
  assert.equal(writes.length, 3);
  assert.equal(writes.some((call) => call.args[1] === "held"), false);
  assert.ok(writes.every((call) => call.args[6].getTime() === Date.parse(payload.snapshot_at)));
  assert.ok(db.calls.includes("commit"));
  assert.equal(db.calls.includes("rollback"), false);
});

test("database failures roll back; replay returns saved result and stale snapshots are rejected", async () => {
  const failing = database({ failWrite: true });
  await assert.rejects(syncSnapshot(snapshot([row("sid", 2)]), failing), /write failed/);
  assert.ok(failing.calls.includes("rollback"));
  assert.ok(failing.calls.includes("release"));
  const previous = { success: true, summary: { created: 1 } };
  const replay = database({ previous: JSON.stringify(previous) });
  assert.deepEqual(await syncSnapshot(snapshot([row("sid", 2)]), replay), previous);
  assert.equal(replay.calls.some((call) => call.sql?.includes("INSERT INTO datin_customer_master")), false);
  const stale = database({ lastSnapshot: "2099-01-01T00:00:00Z" });
  await assert.rejects(syncSnapshot(snapshot([]), stale), (error) => error.statusCode === 409);
});

test("Apps Script maps reordered headers, preserves SID text, and excludes NOT rows", () => {
  const context = vm.createContext({});
  vm.runInContext(fs.readFileSync(require.resolve("../integrations/google-apps-script/datin-sync.gs"), "utf8"), context);
  const headers = ["STATUS", "SID", "SERVICE", "KAT. PLGN", "NAME_CUSTOMER", "CUST_DETAIL", "ADDRESS", "NODE_AKSES", "FSP", "SN ONT / L2SW", "SLOT", "PORT", "ONU", "IP OLT", "SERVER"];
  const rows = context.readDatinRows_([headers, ["OK", "000123", "VPNIP", "K2", "BANK JATIM"], ["NOT", "skip"]]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].sid, "000123");
  assert.equal(rows[0].source_row, 2);
  assert.equal(rows[0].customer_name, "BANK JATIM");
  assert.throws(() => context.readDatinRows_([["SID", "STATUS"]]), /Header hilang/);
});

test("Apps Script retries the identical snapshot and installs only a 10 minute trigger", () => {
  const values = { HDWORK_SYNC_URL: "https://backend.example/sync", HDWORK_SYNC_KEY: "test-key" };
  const properties = {
    getProperty: (key) => values[key],
    setProperty: (key, value) => { values[key] = value; },
    deleteProperty: (key) => { delete values[key]; },
  };
  const headers = ["SID", "SERVICE", "KAT. PLGN", "NAME_CUSTOMER", "CUST_DETAIL", "ADDRESS", "NODE_AKSES", "FSP", "SN ONT / L2SW", "SLOT", "PORT", "ONU", "IP OLT", "SERVER", "STATUS"];
  const requests = [];
  const schedule = [];
  const context = vm.createContext({
    console: { log: () => {} },
    LockService: { getScriptLock: () => ({ tryLock: () => true, waitLock: () => {}, releaseLock: () => {} }) },
    PropertiesService: { getScriptProperties: () => properties },
    SpreadsheetApp: {
      getUi: () => ({ alert: () => {} }),
      openById: (id) => ({ getId: () => id, getSheetByName: () => ({
        getName: () => "Sheet1", getDataRange: () => ({ getDisplayValues: () => [headers, ["0001", "VPNIP", "K2", "BANK JATIM", "", "", "", "", "", "", "", "", "", "SBS", "OK"]] }),
      }) }),
    },
    Utilities: { getUuid: () => "ef67c192-4af4-4598-a765-f4f1fc07c410", sleep: () => {} },
    UrlFetchApp: { fetch: (url, options) => {
      requests.push(options);
      return { getResponseCode: () => requests.length === 1 ? 503 : 200, getContentText: () => JSON.stringify({ success: true, summary: { received: 1, created: 1, updated: 0, unchanged: 0, conflicts: [], invalid_rows: [] } }) };
    } },
    ScriptApp: { getProjectTriggers: () => [], newTrigger: (name) => ({ timeBased: () => ({ everyMinutes: (minutes) => ({ create: () => schedule.push({ name, minutes }) }) }) }) },
  });
  vm.runInContext(fs.readFileSync(require.resolve("../integrations/google-apps-script/datin-sync.gs"), "utf8"), context);
  context.runDatinSync_();
  assert.equal(requests.length, 2);
  assert.equal(requests[0].payload, requests[1].payload);
  assert.equal(JSON.parse(requests[0].payload).rows[0].sid, "0001");
  assert.match(values.HDWORK_LAST_SYNC, /Baru: 1/);
  context.installDatinSyncTrigger();
  assert.deepEqual(schedule, [{ name: "syncDatinScheduled", minutes: 10 }]);
});

test("sheet sync endpoint requires its own API key and validates source before any DB write", async (t) => {
  const previous = process.env.DATIN_SHEET_SYNC_API_KEY;
  process.env.DATIN_SHEET_SYNC_API_KEY = "sheet-test-key";
  const app = express();
  app.use("/api/datin-sheet", require("../src/routes/datin-sheet-sync.routes"));
  const server = app.listen(0, "127.0.0.1");
  await new Promise((resolve) => server.once("listening", resolve));
  t.after(async () => {
    await new Promise((resolve) => server.close(resolve));
    if (previous === undefined) delete process.env.DATIN_SHEET_SYNC_API_KEY;
    else process.env.DATIN_SHEET_SYNC_API_KEY = previous;
  });
  const url = `http://127.0.0.1:${server.address().port}/api/datin-sheet/sync`;
  for (const [key, status] of [[null, 401], ["wrong-key", 403], ["sheet-test-key", 400]]) {
    const headers = { "Content-Type": "application/json" };
    if (key) headers["X-Sheet-Sync-Key"] = key;
    const response = await fetch(url, { method: "POST", headers, body: "{}" });
    assert.equal(response.status, status);
  }
});
