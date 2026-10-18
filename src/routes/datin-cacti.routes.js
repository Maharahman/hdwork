const express = require("express");
const crypto = require("crypto");
const { getHeaderPlan, getDeviceSource, saveHeaderMappings, getDeviceMappings } = require("../services/datin-cacti-headers.service");
const { pilotCapabilities, reservePilot, completePilot } = require("../services/datin-cacti-pilot.service");
const { batchCapabilities, reserveBatch, bindBatchHeader, finishBatch } = require("../services/datin-cacti-batch.service");
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
for (const [path, handler] of [["/batch-reservations", reserveBatch], ["/batch-headers", bindBatchHeader], ["/batch-completions", finishBatch]]) {
  router.post(path, async (req, res, next) => {
    try { res.json(await handler(req.body)); }
    catch (error) { if (error.statusCode) return res.status(error.statusCode).json({ success: false, message: error.message }); next(error); }
  });
}

module.exports = router;
