const assert = require("node:assert/strict");
const test = require("node:test");

const { healthProblems } = require("../src/services/cacti-scheduler.service");

function row(scans) {
  return { last_scan_summary: JSON.stringify(scans) };
}

test("health marks VPN disconnected only when every Cacti scan has a connection failure", () => {
  const problems = healthProblems(row([
    { server: "NODEB SBS", status: "failed", failure_kind: "connection", error: "timeout" },
    { server: "NODEB SBU", status: "failed", failure_kind: "connection", error: "timeout" },
    { server: "DATIN SBS", status: "failed", failure_kind: "connection", error: "timeout" },
    { server: "DATIN SBU", status: "failed", failure_kind: "connection", error: "timeout" },
  ]));

  assert.equal(problems.length, 1);
  assert.match(problems[0], /NODEB SBS/);
  assert.match(problems[0], /DATIN SBU/);
});

test("health stays safe when one Cacti scan succeeds or a login fails", () => {
  assert.deepEqual(healthProblems(row([
    { server: "NODEB SBS", status: "success", failure_kind: null },
    { server: "NODEB SBU", status: "failed", failure_kind: "connection" },
    { server: "DATIN SBS", status: "failed", failure_kind: "connection" },
    { server: "DATIN SBU", status: "failed", failure_kind: "connection" },
  ])), []);

  assert.deepEqual(healthProblems(row([
    { server: "NODEB SBS", status: "failed", failure_kind: "login" },
    { server: "NODEB SBU", status: "failed", failure_kind: "login" },
    { server: "DATIN SBS", status: "failed", failure_kind: "login" },
    { server: "DATIN SBU", status: "failed", failure_kind: "login" },
  ])), []);
});
