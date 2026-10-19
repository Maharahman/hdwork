const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("crypto");
const svc = require("../src/services/datin-cacti-reconcile.service");
const { digest } = require("../src/services/datin-cacti-batch.service");
const now = new Date(), sheet = process.env.DATIN_SHEET_ID || "16tSELEZFzbH7FdI3NFqnNJx17xVf8GXgvZD5ghfxkLE";
const run_id = "12345678-1234-1234-1234-123456789abc", job_id = "22345678-1234-1234-1234-123456789abc", token = "a".repeat(64);
const auth = { run_id, token };
function source() {
  const row = { sid: "0001", source_row: 2, status: "OK", server: "SBS", customer_name: "BANK JATIM", customer_detail: "CAPEM", olt_ip: "172.23.225.42", slot: "0", port: "0", onu: "0", serial_number: "SN" };
  return { spreadsheet_id: sheet, snapshot_at: now, customers: [row], aliases: [], conflicts: [],
    inventory: { version: 1, complete: true, total_rows: 1, safe_for_delete: true, rows: [{ ...row }] },
    mappings: [{ sid: row.sid, server: "SBS", tree_id: 34, host_id: 42, header_id: 10, ownership_status: "VERIFIED", verified_at: now, sync_state: "ACTIVE" }],
    headers: [{ server: "SBS", tree_id: 34, normalized_name: "BANK JATIM", header_id: 10 }], masters: [{ sid: "0001", source_status: "OK" }] };
}
function request(s = source(), operation = "UPDATE") {
  return { ...auth, job_id, sid: "0001", operation, desired: svc.desired(s, "0001").config,
    observed: { mapping: svc.identity(s.mappings[0]), config: { before: true }, option_guard: "guard", header_id: 10, inventory_complete: true, exclusive_tree: true } };
}
function fixture(s = source()) {
  const calls = [], runs = new Map(), jobs = new Map(), archives = [], backups = [];
  const c = { beginTransaction: async () => { calls.push("begin"); backups.push(structuredClone({ s, runs, jobs, archives })); },
    commit: async () => { calls.push("commit"); backups.pop(); }, rollback: async () => { calls.push("rollback"); const b = backups.pop(); Object.assign(s, b.s); runs.clear(); b.runs.forEach((v,k)=>runs.set(k,v)); jobs.clear(); b.jobs.forEach((v,k)=>jobs.set(k,v)); archives.splice(0, archives.length, ...b.archives); }, release() {},
    query: async (sql, args = []) => {
      calls.push({ sql, args });
      if (sql.startsWith("SELECT spreadsheet_id")) return [[{ spreadsheet_id: sheet }]];
      if (sql.includes("SELECT * FROM datin_cacti_sync_runs")) return [[...runs.values()].find(r => r.id === args[0])].map(r => r ? [r] : []);
      if (sql.includes("INSERT INTO datin_cacti_sync_runs")) {
        if ([...runs.values()].some(r => r.active === 1)) throw Object.assign(new Error("duplicate"), { code: "ER_DUP_ENTRY" });
        runs.set(args[0], { id: args[0], spreadsheet_id: args[1], token_hash: args[2], run_status: "RUNNING", active: 1 }); return [{}];
      }
      if (sql.includes("UPDATE datin_cacti_sync_runs SET heartbeat")) return [{}];
      if (sql.includes("UPDATE datin_cacti_sync_runs SET run_status")) { runs.get(args[1]).run_status = "DONE"; runs.get(args[1]).active = null; return [{}]; }
      if (sql.includes("FROM datin_sheet_sources s")) return [s.customers.length ? s.customers.map(r => ({ sid: r.sid, data_json: r, source_row: r.source_row, last_snapshot_at: now, age_seconds: 0 })) : [{ sid: null, last_snapshot_at: now, age_seconds: 0 }]];
      if (sql.includes("FROM datin_institution_aliases")) return [s.aliases.map(a => ({ normalized_name: a.alias, institution_name: a.name }))];
      if (sql.includes("SELECT device_inventory_json")) return [[{ device_inventory_json: s.inventory, device_inventory_snapshot_at: now, last_snapshot_at: now }]];
      if (sql.includes("SELECT sid, server, tree_id, host_id, header_id, ownership_status, verified_at")) return [s.mappings];
      if (sql.includes("SELECT server, tree_id, normalized_name")) return [s.headers];
      if (sql.includes("SELECT sid, source_status")) return [s.masters];
      if (sql.includes("FROM datin_cacti_pilot_jobs")) return [[]];
      if (sql.includes("SELECT * FROM datin_cacti_sync_jobs")) { const j=jobs.get(args[0]); return [j && (!args[1] || j.run_id===args[1]) ? [j] : []]; }
      if (sql.includes("SELECT id FROM datin_cacti_sync_jobs")) return [[...jobs.values()].filter(j => j.run_id===args[0] && ["PREPARED","ATTEMPTED"].includes(j.job_status) && (!args[1] || j.resource_key===args[1]))];
      if (sql.includes("SELECT data_json")) { const m=s.masters.find(m=>m.sid===args[1]); return [m ? [{ data_json: { sid: m.sid, backup: true }, source_status: m.source_status }] : []]; }
      if (sql.includes("INSERT INTO datin_cacti_sync_jobs")) { const [id,run_id,spreadsheet_id,resource_key,sid,operation,request_hash,request_json,expected_json,master_backup_json,source_snapshot_at]=args; jobs.set(id,{id,run_id,spreadsheet_id,resource_key,sid,operation,request_hash,request_json,expected_json,master_backup_json,source_snapshot_at,job_status:"PREPARED"}); return [{}]; }
      if (sql.includes("UPDATE datin_cacti_sync_jobs SET job_status = 'ATTEMPTED'")) {jobs.get(args[0]).job_status="ATTEMPTED";return [{}];}
      if (sql.includes("UPDATE datin_cacti_sync_jobs SET job_status = 'CANCELLED'")) {jobs.get(args[0]).job_status="CANCELLED";return [{}];}
      if (sql.includes("UPDATE datin_cacti_sync_jobs SET job_status = 'VERIFIED'")) {const j=jobs.get(args[1]);j.job_status="VERIFIED";j.receipt_json=args[0];return [{}];}
      if (sql.includes("SELECT sid, server, tree_id, host_id")) return [s.mappings.filter(m=>m.sid===args[1])];
      if (sql.includes("SET sync_state = 'DELETED'")) {s.mappings[0].sync_state="DELETED";return [{}];}
      if (sql.includes("UPDATE datin_cacti_devices SET server")) {Object.assign(s.mappings[0],{server:args[0],tree_id:args[1],host_id:args[2],header_id:args[3],sync_state:"ACTIVE",last_applied_json:JSON.parse(args[4])});return [{}];}
      if (sql.includes("INSERT INTO datin_customer_archives")) {archives.push(args);return [{}];}
      if (sql.includes("DELETE FROM datin_customer_master")) {s.masters.splice(0,s.masters.length,...s.masters.filter(m=>m.sid!==args[1]||m.source_status!=="NOT"));return [{affectedRows:1}];}
      throw new Error("Unexpected SQL: "+sql);
    } };
  return { database: { getConnection: async () => c }, calls, runs, jobs, archives, source: s };
}
async function enabled(fn) { const old=process.env.DATIN_CACTI_AUTOMATION_ENABLED;process.env.DATIN_CACTI_AUTOMATION_ENABLED="1";try{return await fn();}finally{if(old===undefined)delete process.env.DATIN_CACTI_AUTOMATION_ENABLED;else process.env.DATIN_CACTI_AUTOMATION_ENABLED=old;} }

test("semantic eligibility accepts zero; missing/conflict/NOT/ATCS never authorizes update/create", () => {
  const s=source(); assert.equal(svc.desired(s,"0001").config.slot,"0");
  for(const mutation of [s=>s.inventory.rows=[],s=>s.inventory.rows.push({...s.inventory.rows[0]}),s=>s.inventory.rows[0].status="NOT",s=>s.inventory.rows[0].customer_name="ATCS"]){const s=source();mutation(s);assert.throws(()=>svc.desired(s,"0001"));}
  const p=request(); assert.equal(svc.expectedFor(s,p).mapping.host_id,42);
  assert.throws(()=>svc.expectedFor(s,{...p,observed:{...p.observed,mapping:{...p.observed.mapping,host_id:3199}}}));
});
test("NOT needs unique exact SID, valid server/header, shrink guard and bounded delete count", () => {
  const s=source();s.inventory.rows[0].status="NOT";svc.deletionEligible(s,"0001",s.mappings[0]);
  for(const mutation of [s=>s.inventory.rows=[],s=>s.inventory.safe_for_delete=false,s=>s.inventory.rows[0].server="SBU",s=>s.inventory.rows.push({...s.inventory.rows[0],status:"OK"}),s=>s.inventory.rows[0].customer_name="ATCS"]){const x=structuredClone(s);mutation(x);assert.throws(()=>svc.deletionEligible(x,"0001",x.mappings[0]));}
  for(let i=2;i<=12;i++){s.inventory.rows.push({...s.inventory.rows[0],sid:String(i)});s.mappings.push({...s.mappings[0],sid:String(i)});}
  assert.throws(()=>svc.deletionEligible(s,"0001",s.mappings[0]),/batas/);
});
test("backend run token is hashed, replay safe and another active run cannot start", async () => enabled(async()=>{
  const f=fixture();await svc.startRun({...auth,spreadsheet_id:sheet},f.database);
  assert.notEqual(f.runs.get(run_id).token_hash,token);
  assert.equal((await svc.startRun({...auth,spreadsheet_id:sheet},f.database)).replay,true);
  await assert.rejects(svc.startRun({...auth,run_id:crypto.randomUUID(),spreadsheet_id:sheet},f.database));
  await assert.rejects(svc.startRun({...auth,token:"b".repeat(64)},f.database));
}));
test("job prepare replay and lost authorize response never grant a second write", async () => enabled(async()=>{
  const f=fixture();await svc.startRun({...auth,spreadsheet_id:sheet},f.database);const p=request(f.source);
  await svc.prepareJob(p,f.database);await svc.prepareJob(p,f.database);
  assert.equal(f.jobs.size,1);
  assert.equal((await svc.attemptJob(p,f.database)).may_write,true);
  assert.equal((await svc.attemptJob(p,f.database)).may_write,false);
  await assert.rejects(svc.cancelJob(p,f.database));
  await assert.rejects(svc.finishRun(auth,f.database));
}));
test("changed source before authorization rolls back and PREPARED job can be cancelled", async () => enabled(async()=>{
  const f=fixture();await svc.startRun({...auth,spreadsheet_id:sheet},f.database);const p=request(f.source);await svc.prepareJob(p,f.database);
  f.source.customers[0].slot="1";
  await assert.rejects(svc.attemptJob(p,f.database));
  assert.equal(f.jobs.get(job_id).job_status,"PREPARED");
  await svc.cancelJob(p,f.database);await svc.finishRun(auth,f.database);
  assert.equal(f.runs.get(run_id).run_status,"DONE");
}));
test("authorized job can cancel only with explicit agent attestation of no mutation intent", async () => enabled(async()=>{
  const f=fixture();await svc.startRun({...auth,spreadsheet_id:sheet},f.database);const p=request(f.source);
  await svc.prepareJob(p,f.database);await svc.attemptJob(p,f.database);
  await assert.rejects(svc.cancelJob({...p,no_cacti_write:true},f.database));
  await svc.cancelJob({...p,no_cacti_write:true,journal_proof:"NO_MUTATION_INTENT"},f.database);
  assert.equal(f.jobs.get(job_id).job_status,"CANCELLED");
}));
test("verified update changes same mapping and receipt replay never repeats database changes", async () => enabled(async()=>{
  const f=fixture();await svc.startRun({...auth,spreadsheet_id:sheet},f.database);const p=request(f.source);await svc.prepareJob(p,f.database);await svc.attemptJob(p,f.database);
  const receipt={config:p.desired,server:"SBS",tree_id:34,header_id:10,host_id:42,monitor:true,disabled:false,exclusive_tree:true,option_guard:"guard"};
  await svc.completeJob({...p,receipt},f.database);assert.equal(f.source.mappings[0].host_id,42);
  const count=f.calls.filter(c=>c.sql?.includes("UPDATE datin_cacti_devices")).length;
  assert.equal((await svc.completeJob({...p,receipt},f.database)).replay,true);
  assert.equal(f.calls.filter(c=>c.sql?.includes("UPDATE datin_cacti_devices")).length,count);
  await assert.rejects(svc.completeJob({...p,receipt:{...receipt,host_id:3199}},f.database));
}));
test("verified NOT deletion tombstones mapping, archives and purges only exact NOT master", async () => enabled(async()=>{
  const s=source();s.inventory.rows[0].status="NOT";s.customers=[];s.masters[0].source_status="NOT";
  const f=fixture(s);await svc.startRun({...auth,spreadsheet_id:sheet},f.database);
  const p={...auth,job_id,sid:"0001",operation:"DELETE",observed:{mapping:svc.identity(s.mappings[0]),absent:true,inventory_complete:true}};
  await svc.prepareJob(p,f.database);await svc.attemptJob(p,f.database);
  const receipt={server:"SBS",host_id:42,absent:true,inventory_complete:true,other_host_ids_unchanged:true,headers_unchanged:true,no_tree_references:true};
  await svc.completeJob({...p,receipt},f.database);
  assert.equal(s.mappings[0].sync_state,"DELETED");assert.equal(f.archives.length,1);assert.equal(s.masters.length,0);
  assert.equal((await svc.completeJob({...p,receipt},f.database)).replay,true);assert.equal(f.archives.length,1);
}));
test("NOT reverting to OK during remote delete records deletion but never purges new OK master", async () => enabled(async()=>{
  const s=source(), customer=s.customers[0];s.inventory.rows[0].status="NOT";s.customers=[];s.masters[0].source_status="NOT";
  const f=fixture(s);await svc.startRun({...auth,spreadsheet_id:sheet},f.database);
  const p={...auth,job_id,sid:"0001",operation:"DELETE",observed:{mapping:svc.identity(s.mappings[0]),absent:true,inventory_complete:true}};
  await svc.prepareJob(p,f.database);await svc.attemptJob(p,f.database);
  s.inventory.rows[0].status="OK";s.customers=[customer];s.masters[0].source_status="OK";
  await svc.completeJob({...p,receipt:{server:"SBS",host_id:42,absent:true,inventory_complete:true,other_host_ids_unchanged:true,headers_unchanged:true,no_tree_references:true}},f.database);
  assert.equal(s.mappings[0].sync_state,"DELETED");assert.equal(s.masters.length,1);assert.equal(f.archives.length,0);
}));
