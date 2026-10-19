const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { prepareDeviceInventory, saveDeviceInventory } = require("../src/services/datin-device-inventory.service");
const { prepareSnapshot } = require("../src/services/datin-sheet-sync.service");
function payload() {
  const rows = [{ sid: "0001", source_row: 2, status: "OK", server: "SBS", customer_name: "BANK JATIM", slot: "0", port: "0", onu: "0" }];
  return { spreadsheet_id: "16tSELEZFzbH7FdI3NFqnNJx17xVf8GXgvZD5ghfxkLE", sheet_name: "Sheet1", run_id: "ef67c192-4af4-4598-a765-f4f1fc07c410", snapshot_at: new Date().toISOString(), rows,
    device_inventory: { version: 1, complete: true, total_rows: 2, blank_rows: 0, rows: [{ ...rows[0] }, { sid: "0002", source_row: 3, status: "NOT", server: "SBU", customer_name: "BANK JATIM" }] } };
}
test("full SID inventory preserves NOT and text SID; missing old protocol never means NOT", () => {
  const p = payload(), inv = prepareDeviceInventory(p);
  assert.equal(inv.rows[1].sid, "0002");
  assert.equal(inv.rows[1].status, "NOT");
  assert.equal(prepareDeviceInventory({}), null);
  assert.equal(prepareSnapshot(p).records[0].data.slot, "0");
});
test("complete inventory rejects row holes, duplicates and mismatched OK data", () => {
  for (const mutate of [p => p.device_inventory.total_rows++, p => p.device_inventory.rows[1].source_row = 2,
    p => p.device_inventory.rows[0].sid = "different", p => p.device_inventory.rows[0].server = "SBU",
    p => p.device_inventory.rows[1].status = "OK", p => p.device_inventory.rows[1].sid = 123]) {
    const p = structuredClone(payload()); mutate(p); assert.throws(() => prepareDeviceInventory(p));
  }
});
test("duplicate SID across OK and NOT is held, not upserted or deletion-authorized", () => {
  const p = payload(); p.device_inventory.rows[1].sid = "0001";
  const result = prepareSnapshot(p);
  assert.equal(result.records.length, 0);
  assert.deepEqual(result.summary.conflicts, [{ sid: "0001", rows: [2, 3] }]);
});
test("old/partial snapshots cannot replace NOT evidence; 50% shrink disables delete", async () => {
  const calls = [];
  const c = { query: async (sql, args) => { calls.push({ sql, args }); return sql.startsWith("SELECT") ? [[{ device_inventory_json: { total_rows: 10, guard_rows: 10 } }]] : [{}]; } };
  const p = payload(), inv = prepareDeviceInventory(p);
  assert.equal((await saveDeviceInventory(c, p, null, [], new Date())).status, "SKIPPED");
  assert.equal((await saveDeviceInventory(c, p, { ...inv, complete: false }, [], new Date())).status, "INCOMPLETE");
  assert.equal(calls.length, 0);
  const summary = await saveDeviceInventory(c, p, inv, prepareSnapshot(p).records, new Date());
  assert.equal(summary.safe_for_delete, false);
  assert.equal(summary.not, 1);
  assert.ok(calls.some(c => c.sql.includes("source_status = 'NOT'")));
  assert.ok(calls.every(c => !c.sql.includes("DELETE FROM")));
});
test("Apps Script transmits SID/status inventory for all rows alongside only OK customer data", () => {
  const ctx = vm.createContext({});
  vm.runInContext(fs.readFileSync(require.resolve("../integrations/google-apps-script/datin-sync.gs"), "utf8"), ctx);
  const headers = ["STATUS", "SID", "SERVICE", "KAT. PLGN", "NAME_CUSTOMER", "CUST_DETAIL", "ADDRESS", "NODE_AKSES", "FSP", "SN ONT / L2SW", "SLOT", "PORT", "ONU", "IP OLT", "SERVER"];
  const grid = [headers, ["OK", "0001", "VPNIP", "K2", "BANK JATIM", "", "", "", "", "SN", 0, 0, 0, "172.23.225.42", "SBS"],
    ["NOT", "0002", "VPNIP", "K2", "BANK JATIM", "", "", "", "", "SN", 0, 0, 0, "172.23.225.42", "SBU"]];
  const contents = JSON.parse(JSON.stringify(ctx.readDatinSnapshot_(grid)));
  assert.equal(contents.rows.length, 1);
  assert.equal(contents.device_inventory.rows.length, 2);
  assert.equal(contents.device_inventory.rows[1].sid, "0002");
  assert.equal(prepareDeviceInventory(contents).rows[1].status, "NOT");
});
