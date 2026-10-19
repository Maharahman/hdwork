const express = require("express");
const crypto = require("crypto");
const { getHeaderPlan, getDeviceSource, saveHeaderMappings, getDeviceMappings } = require("../services/datin-cacti-headers.service");
const { pilotCapabilities, reservePilot, completePilot } = require("../services/datin-cacti-pilot.service");
const { batchCapabilities, reserveBatch, bindBatchHeader, finishBatch } = require("../services/datin-cacti-batch.service");
const { reserveBulk } = require("../services/datin-cacti-bulk.service");
const reconcile = require("../services/datin-cacti-reconcile.service");
const router = express.Router();

router.use((req, res, next) => {
  const configured = process.env.DATIN_CACTI_AGENT_API_KEY;
  if (!configured) return res.status(503).json({ success: false, message: "Agent Cacti belum dikonfigurasi." });
  const supplied = req.get("X-Cacti-Agent-Key");
  if (!supplied) return res.status(401).json({ success: false, message: "API key agent diperlukan." });
  const expected = Buffer.from(configured), actual = Buffer.from(supplied);
  if (actual.length !== expected.length || !crypto.timingSafeEqual(actual, expected)) return res.status(403).json({ success: false, message: "API key agent tidak valid." });
  next();
});

// Once automation owns the workflow, legacy reservation/mapping writers must
// not race it. Existing completed pilots remain available as read-only history.
router.use((req, res, next) => {
  if (reconcile.enabled() && req.method === "POST" && !req.path.startsWith("/sync/")) return res.status(409).json({ success: false, message: "Otomatisasi aktif; jangan jalankan writer pilot/bulk/header lama." });
  next();
});
router.get("/sync/capabilities", (req, res) => res.json({ success: true, version: 1, enabled: reconcile.enabled(), interval_seconds: 600, max_deletes_per_run: reconcile.deleteLimit() }));
router.get("/sync/source", async (req, res, next) => {
  try { res.json(await reconcile.getSyncSource()); }
  catch (error) { if (error.statusCode) return res.status(error.statusCode).json({ success: false, message: error.message }); next(error); }
});
for (const [path, handler] of [["/sync/runs", reconcile.startRun], ["/sync/jobs/prepare", reconcile.prepareJob], ["/sync/jobs/attempt", reconcile.attemptJob], ["/sync/jobs/cancel", reconcile.cancelJob], ["/sync/jobs/complete", reconcile.completeJob], ["/sync/runs/complete", reconcile.finishRun], ["/sync/runs/error", reconcile.noteRunError]]) {
  router.post(path, async (req, res, next) => {
    try { res.json(await handler(req.body)); }
    catch (error) { if (error.statusCode) return res.status(error.statusCode).json({ success: false, message: error.message, safe_to_replan: error.safeToReplan === true }); next(error); }
  });
}

router.get("/header-plan", async (req, res, next) => {
  try { res.json(await getHeaderPlan()); }
  catch (error) { if (error.statusCode) return res.status(error.statusCode).json({ success: false, message: error.message }); next(error); }
});
router.get("/device-source", async (req, res, next) => {
  try { res.json(await getDeviceSource()); }
  catch (error) { if (error.statusCode) return res.status(error.statusCode).json({ success: false, message: error.message }); next(error); }
});
router.get("/device-mappings", async (req, res, next) => {
  try { res.json(await getDeviceMappings()); }
  catch (error) { if (error.statusCode) return res.status(error.statusCode).json({ success: false, message: error.message }); next(error); }
});
router.post("/header-mappings", async (req, res, next) => {
  try { res.json(await saveHeaderMappings(req.body)); }
  catch (error) { if (error.statusCode) return res.status(error.statusCode).json({ success: false, message: error.message }); next(error); }
});

router.get("/pilot-capabilities", (req, res) => res.json(pilotCapabilities()));
router.post("/pilot-reservations", async (req, res, next) => {
  try { res.json(await reservePilot(req.body)); }
  catch (error) { if (error.statusCode) return res.status(error.statusCode).json({ success: false, message: error.message }); next(error); }
});
router.post("/pilot-completions", async (req, res, next) => {
  try { res.json(await completePilot(req.body)); }
  catch (error) { if (error.statusCode) return res.status(error.statusCode).json({ success: false, message: error.message }); next(error); }
});

router.get("/batch-capabilities", (req, res) => res.json(batchCapabilities()));
router.get("/bulk-capabilities", (req, res) => res.json({ success: true, version: 1, max_devices: 1000, resumable: true, create_only: true }));
router.post("/bulk-reservations", async (req, res, next) => {
  try { res.json(await reserveBulk(req.body)); }
  catch (error) { if (error.statusCode) return res.status(error.statusCode).json({ success: false, message: error.message }); next(error); }
});
for (const [path, handler] of [["/batch-reservations", reserveBatch], ["/batch-headers", bindBatchHeader], ["/batch-completions", finishBatch]]) {
  router.post(path, async (req, res, next) => {
    try { res.json(await handler(req.body)); }
    catch (error) { if (error.statusCode) return res.status(error.statusCode).json({ success: false, message: error.message }); next(error); }
  });
}

module.exports = router;
