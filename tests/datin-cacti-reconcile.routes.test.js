const test = require("node:test");
const assert = require("node:assert/strict");
const express = require("express");
test("reconcile endpoints require agent key, disabled mode cannot write, and active mode fences legacy writers", async t => {
  const names=["DATIN_CACTI_AGENT_API_KEY","DATIN_CACTI_AUTOMATION_ENABLED"];
  const old=Object.fromEntries(names.map(n=>[n,process.env[n]]));
  process.env.DATIN_CACTI_AGENT_API_KEY="test-agent-key";process.env.DATIN_CACTI_AUTOMATION_ENABLED="0";
  const app=express();app.use(express.json());app.use("/api/datin-cacti",require("../src/routes/datin-cacti.routes"));
  const server=app.listen(0,"127.0.0.1");await new Promise(r=>server.once("listening",r));
  t.after(async()=>{await new Promise(r=>server.close(r));for(const n of names){if(old[n]===undefined)delete process.env[n];else process.env[n]=old[n];}});
  const base=`http://127.0.0.1:${server.address().port}/api/datin-cacti`;
  for(const [key,status] of [[null,401],["wrong",403],["test-agent-key",200]]){
    const response=await fetch(base+"/sync/capabilities",{headers:key?{"X-Cacti-Agent-Key":key}:{}});assert.equal(response.status,status);
    if(status===200)assert.equal((await response.json()).enabled,false);
  }
  const headers={"Content-Type":"application/json","X-Cacti-Agent-Key":"test-agent-key"};
  const body=JSON.stringify({run_id:"12345678-1234-1234-1234-123456789abc",token:"a".repeat(64)});
  assert.equal((await fetch(base+"/sync/runs",{method:"POST",headers,body})).status,503);
  process.env.DATIN_CACTI_AUTOMATION_ENABLED="1";
  for(const endpoint of ["/bulk-reservations","/pilot-reservations","/header-mappings"]){assert.equal((await fetch(base+endpoint,{method:"POST",headers,body:"{}"})).status,409);}
});
