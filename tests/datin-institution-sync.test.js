const test = require("node:test");
const assert = require("node:assert/strict");
const { prepareInstitutionInventory } = require("../src/services/datin-institution-sync.service");
const { prepareSnapshot, syncSnapshot } = require("../src/services/datin-sheet-sync.service");
const { getDatinInstitutions, toggleDatinSubscription } = require("../src/repositories/telegram.repository");

const names = ["BANK CENTRAL ASIA TBK (BANK BCA)", "BANK DANAMON INDONESIA TBK", "BANK JATIM", "HELD CONFLICT", "HELD NOT", "OBSOLETE"];
const row = (sid, source_row, customer_name, status = "OK") => ({ sid, source_row, customer_name, status });
function snapshot(rows = [row("bca", 2, "BANK CENTRAL ASIA"), row("dan", 3, "BANK DANAMON"), row("disk", 4, "DISKOMINFO KAB SUMENEP"), row("jatim", 5, "BANK JATIM"), row("conflict", 6, "HELD CONFLICT"), row("conflict", 7, "HELD CONFLICT")], extras = [{ source_row: 8, customer_name: "HELD NOT", status: "NOT" }]) {
  const inventory = [...rows.map(({ source_row, customer_name, status }) => ({ source_row, customer_name, status })), ...extras];
  return { spreadsheet_id: "16tSELEZFzbH7FdI3NFqnNJx17xVf8GXgvZD5ghfxkLE", sheet_name: "Sheet1", run_id: "ef67c192-4af4-4598-a765-f4f1fc07c410", snapshot_at: new Date().toISOString(), rows,
    institution_inventory: { version: 1, complete: true, total_rows: inventory.length, blank_rows: 0, rows: inventory } };
}

function database(options = {}) {
  let state = { masters: names.map((name, i) => ({ id: i + 1, name, is_active: i === 1 ? 0 : 1 })),
    aliases: names.map((name, i) => ({ institution_id: i + 1, normalized_name: name })),
    subscriptions: [{ institution_id: 1, telegram_user_id: 100 }, { institution_id: 3, telegram_user_id: 100 }, { institution_id: 6, telegram_user_id: 100 }], runs: {}, last: null };
  const calls = [];
  let before;
  const connection = {
    beginTransaction: async () => { calls.push("begin"); before = structuredClone(state); },
    commit: async () => calls.push("commit"), rollback: async () => { calls.push("rollback"); state = before; }, release: () => calls.push("release"),
    query: async (sql, args = []) => {
      calls.push({ sql, args });
      if (sql.startsWith("SELECT last_snapshot_at")) return [[{ last_snapshot_at: state.last }]];
      if (sql.startsWith("SELECT result_json") && sql.includes("run_id =")) return [state.runs[args[0]] ? [{ result_json: state.runs[args[0]] }] : []];
      if (sql.startsWith("SELECT result_json")) return options.baseline ? [[{ result_json: { summary: { institutions: options.baseline } } }]] : [[]];
      if (sql.startsWith("SELECT sid")) return [[]];
      if (sql.startsWith("SELECT id, name")) return [structuredClone(state.masters)];
      if (sql.startsWith("SELECT institution_id")) return [structuredClone(state.aliases)];
      if (sql.startsWith("INSERT INTO datin_institutions")) {
        const id = Math.max(0, ...state.masters.map((row) => Number(row.id))) + 1;
        state.masters.push({ id, name: args[0], is_active: 1 });
        return [{ insertId: id }];
      }
      if (sql.startsWith("INSERT INTO datin_institution_aliases")) { state.aliases.push({ institution_id: args[0], normalized_name: args[2] }); return [{}]; }
      if (sql.startsWith("UPDATE datin_institutions")) { state.masters.find((row) => String(row.id) === args[0]).is_active = 1; return [{}]; }
      if (sql.startsWith("DELETE")) {
        if (options.failDelete && sql.includes("datin_institution_aliases")) throw new Error("delete failed");
        const key = sql.includes("telegram_datin_subscriptions") ? "subscriptions" : sql.includes("datin_institution_aliases") ? "aliases" : "masters";
        const field = key === "masters" ? "id" : "institution_id", count = state[key].length;
        state[key] = state[key].filter((row) => !args.includes(String(row[field])));
        return [{ affectedRows: count - state[key].length }];
      }
      if (sql.startsWith("INSERT INTO datin_sheet_sync_runs")) state.runs[args[0]] = args[3];
      if (sql.startsWith("UPDATE datin_sheet_sources")) state.last = args[0];
      return [{}];
    },
  };
  return { connection, calls, getConnection: async () => connection, state: () => structuredClone(state) };
}

test("inventory includes NOT/conflict names and rejects incomplete/mismatched row metadata", () => {
  const payload = snapshot();
  const prepared = prepareSnapshot(payload);
  assert.ok(prepared.institutions.names.has("HELD NOT"));
  assert.ok(prepared.institutions.names.has("HELD CONFLICT"));
  assert.equal(prepared.records.length, 4);
  for (const change of [{ version: 2 }, { total_rows: 99 }, { rows: payload.institution_inventory.rows.slice(1) }, { rows: [...payload.institution_inventory.rows.slice(0, -1), payload.institution_inventory.rows[0]] }]) {
    assert.throws(() => prepareSnapshot({ ...payload, institution_inventory: { ...payload.institution_inventory, ...change } }));
  }
  const mismatched = structuredClone(payload);
  mismatched.institution_inventory.rows[0].customer_name = "WRONG";
  assert.throws(() => prepareSnapshot(mismatched), /berbeda/);
});

test("aliases preserve master/subscriber IDs, create DISKOMINFO, and delete only truly absent masters", async () => {
  const db = database(), before = db.state();
  const result = await syncSnapshot(snapshot(), db);
  const summary = result.summary.institutions, after = db.state();
  assert.equal(summary.created, 1);
  assert.equal(summary.reactivated, 1);
  assert.equal(summary.deleted, 1);
  assert.equal(summary.subscriptions_deleted, 1);
  assert.deepEqual(summary.removed, [{ id: "6", name: "OBSOLETE" }]);
  assert.equal(after.masters.find((row) => row.name === names[0]).id, before.masters[0].id);
  assert.ok(after.aliases.some((row) => row.normalized_name === "BANK CENTRAL ASIA" && String(row.institution_id) === "1"));
  assert.ok(after.aliases.some((row) => row.normalized_name === "BANK DANAMON" && String(row.institution_id) === "2"));
  assert.ok(after.masters.some((row) => row.name === "DISKOMINFO KAB SUMENEP"));
  assert.ok(after.masters.some((row) => row.name === "HELD NOT"));
  assert.ok(after.masters.some((row) => row.name === "HELD CONFLICT"));
  assert.deepEqual(after.subscriptions.map((row) => row.institution_id), [1, 3]);
  const deletes = db.calls.filter((call) => call.sql?.startsWith("DELETE")).map((call) => call.sql);
  assert.match(deletes[0], /telegram_datin_subscriptions/);
  assert.match(deletes[1], /datin_institution_aliases/);
  assert.match(deletes[2], /datin_institutions/);
  assert.ok(!db.calls.some((call) => /DELETE.*(?:cacti_alarms|datin_cacti|datin_customer_master)/.test(call.sql || "")));
});

test("replay never repeats cleanup and institution errors roll back the whole snapshot", async () => {
  const payload = snapshot(), db = database();
  const first = await syncSnapshot(payload, db), state = db.state();
  const replay = await syncSnapshot(payload, db);
  assert.deepEqual(replay, first);
  assert.deepEqual(db.state(), state);
  assert.equal(db.calls.filter((call) => call.sql?.startsWith("DELETE FROM datin_institutions")).length, 1);
  const failing = database({ failDelete: true }), original = failing.state();
  await assert.rejects(syncSnapshot(payload, failing), /delete failed/);
  assert.deepEqual(failing.state(), original);
  assert.ok(failing.calls.includes("rollback"));
});

test("empty, partial, nameless, all-held and drastically smaller snapshots cannot prune", async () => {
  const candidates = [snapshot([], []), { ...snapshot(), institution_inventory: { ...snapshot().institution_inventory, complete: false } }, snapshot([row("x", 2, "BANK JATIM")], [{ source_row: 3, customer_name: "", status: "NOT" }]), snapshot([], [{ source_row: 2, customer_name: "HELD NOT", status: "NOT" }])];
  for (const payload of candidates) {
    const db = database();
    const result = await syncSnapshot(payload, db);
    assert.ok(result.summary.institutions.delete_skipped_reason);
    assert.equal(result.summary.institutions.deleted, 0);
    assert.ok(!db.calls.some((call) => call.sql?.startsWith("DELETE")));
  }
  const db = database({ baseline: { guard_rows: 100, guard_names: 20 } });
  const result = await syncSnapshot(snapshot([row("x", 2, "BANK JATIM")], []), db);
  assert.match(result.summary.institutions.delete_skipped_reason, /50%/);
  assert.equal(result.summary.institutions.guard_rows, 100);
  assert.equal(result.summary.institutions.guard_names, 20);
});

test("old Apps Script cannot change/prune master institutions", async () => {
  const payload = snapshot();
  delete payload.institution_inventory;
  const db = database(), before = db.state();
  const result = await syncSnapshot(payload, db);
  assert.equal(result.summary.institutions.status, "SKIPPED");
  assert.deepEqual(db.state().masters, before.masters);
  assert.deepEqual(db.state().subscriptions, before.subscriptions);
  assert.equal(prepareInstitutionInventory(payload), null);
});

test("ambiguous short/full masters are never silently merged or reassigned", async () => {
  const db = database();
  const originalQuery = db.connection.query;
  db.connection.query = async (sql, args) => {
    if (sql.startsWith("SELECT id, name")) return [[{ id: 1, name: names[0], is_active: 1 }, { id: 99, name: "BANK CENTRAL ASIA", is_active: 1 }]];
    if (sql.startsWith("SELECT institution_id")) return [[]];
    return originalQuery(sql, args);
  };
  await assert.rejects(syncSnapshot(snapshot(), db), (error) => error.statusCode === 409 && /ganda/.test(error.message));
  assert.ok(db.calls.includes("rollback"));
});

test("names with only NOT/conflicting SID rows protect presence but never create a new master", async () => {
  const payload = snapshot();
  payload.rows.push(row("held-new", 9, "ONLY CONFLICT NEW"), row("held-new", 10, "ONLY CONFLICT NEW"));
  payload.institution_inventory.rows.push(
    { source_row: 9, customer_name: "ONLY CONFLICT NEW", status: "OK" },
    { source_row: 10, customer_name: "ONLY CONFLICT NEW", status: "OK" },
    { source_row: 11, customer_name: "ONLY NOT NEW", status: "NOT" },
  );
  payload.institution_inventory.total_rows = 10;
  const db = database();
  const result = await syncSnapshot(payload, db);
  assert.equal(result.summary.institutions.created, 1); // DISKOMINFO only.
  assert.ok(!db.state().masters.some((row) => row.name.startsWith("ONLY")));
});

test("pagination clamps stale Telegram pages after master cleanup", async () => {
  let offset;
  const db = { query: async (sql, args) => {
    if (sql.includes("COUNT")) return [[{ total: 2 }]];
    offset = args[1]; return [[{ id: 1, name: "BANK JATIM" }]];
  } };
  const result = await getDatinInstitutions(99, 8, db);
  assert.equal(result.page, 0);
  assert.equal(offset, 0);
});

test("stale keyboard cannot subscribe to a removed master; toggles lock live masters", async () => {
  for (const present of [true, false]) {
    const calls = [];
    const c = { beginTransaction: async () => calls.push("begin"), commit: async () => calls.push("commit"), rollback: async () => calls.push("rollback"), release: () => calls.push("release"), query: async (sql) => {
      calls.push(sql);
      if (sql.startsWith("SELECT id")) return [present ? [{ id: 1 }] : []];
      if (sql.startsWith("SELECT is_active")) return [[{ is_active: 1 }]];
      return [{}];
    } };
    const result = await toggleDatinSubscription(100, 1, { getConnection: async () => c });
    assert.equal(result, present ? false : null);
    assert.equal(calls.some((call) => call.includes("INSERT INTO telegram_datin_subscriptions")), present);
    assert.ok(calls.some((call) => call.includes("FOR UPDATE")));
    assert.ok(calls.includes("commit"));
  }
});
