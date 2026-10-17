const express = require("express");
const crypto = require("crypto");
const { syncSnapshot } = require("../services/datin-sheet-sync.service");
const router = express.Router();

function verifySheetSync(req, res, next) {
  const configured = process.env.DATIN_SHEET_SYNC_API_KEY;
  if (!configured) return res.status(503).json({ success: false, message: "Sinkronisasi spreadsheet belum dikonfigurasi." });
  const supplied = req.get("X-Sheet-Sync-Key");
  if (!supplied) return res.status(401).json({ success: false, message: "API key sinkronisasi diperlukan." });
  const expected = Buffer.from(configured);
  const actual = Buffer.from(supplied);
  if (actual.length !== expected.length || !crypto.timingSafeEqual(actual, expected)) {
    return res.status(403).json({ success: false, message: "API key sinkronisasi tidak valid." });
  }
  next();
}

router.post("/sync", verifySheetSync, express.json({ limit: "5mb" }), async (req, res, next) => {
  try {
    res.json(await syncSnapshot(req.body));
  } catch (error) {
    if (error.statusCode) return res.status(error.statusCode).json({ success: false, message: error.message });
    next(error);
  }
});

router.use((error, req, res, next) => {
  if (error.type === "entity.too.large" || error.type === "entity.parse.failed") {
    const status = error.type === "entity.too.large" ? 413 : 400;
    return res.status(status).json({ success: false, message: status === 413 ? "Payload melebihi batas 5 MB." : "Payload JSON tidak valid." });
  }
  next(error);
});

module.exports = router;
