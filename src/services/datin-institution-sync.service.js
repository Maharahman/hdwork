const normalize = (value) => String(value ?? "").trim().replace(/\s+/g, " ").toUpperCase();
const fail = (message, statusCode = 400) => Object.assign(new Error(message), { statusCode });
const KNOWN_ALIASES = {
  "BANK CENTRAL ASIA": "BANK CENTRAL ASIA TBK (BANK BCA)",
  "BANK DANAMON": "BANK DANAMON INDONESIA TBK",
};

function prepareInstitutionInventory(payload) {
  const inventory = payload.institution_inventory;
  if (inventory === undefined) return null; // Old Apps Script must never prune masters.
  if (!inventory || inventory.version !== 1 || typeof inventory.complete !== "boolean"
      || !Number.isSafeInteger(inventory.total_rows) || inventory.total_rows < 0 || inventory.total_rows > 10000
      || !Number.isSafeInteger(inventory.blank_rows) || inventory.blank_rows < 0
      || !Array.isArray(inventory.rows) || inventory.rows.length + inventory.blank_rows !== inventory.total_rows) {
    throw fail("Inventaris seluruh instansi Sheet1 tidak lengkap/valid.");
  }
  const byRow = new Map(), names = new Set();
  let missingNames = 0;
  for (const row of inventory.rows) {
    if (!row || !Number.isSafeInteger(row.source_row) || row.source_row < 2 || row.source_row > inventory.total_rows + 1 || byRow.has(row.source_row)
        || typeof row.customer_name !== "string" || row.customer_name.length > 255 || typeof row.status !== "string" || row.status.length > 32) {
      throw fail("Baris inventaris instansi tidak valid/ganda.");
    }
    byRow.set(row.source_row, row);
    if (normalize(row.customer_name)) names.add(normalize(row.customer_name));
    else missingNames++;
  }
  const okRows = payload.rows.filter((row) => normalize(row.status) === "OK");
  const rowIds = new Set();
  if (okRows.length !== inventory.rows.filter((row) => normalize(row.status) === "OK").length) throw fail("Jumlah baris OK dan inventaris instansi berbeda.");
  for (const row of okRows) {
    const entry = byRow.get(row.source_row);
    if (!entry || rowIds.has(row.source_row) || normalize(entry.status) !== "OK" || normalize(entry.customer_name) !== normalize(row.customer_name)) {
      throw fail("Data pelanggan dan inventaris instansi berbeda.");
    }
    rowIds.add(row.source_row);
  }
  return { names, totalRows: inventory.total_rows, missingNames, complete: inventory.complete };
}

async function syncInstitutions(connection, inventory, records, baseline = {}) {
  const summary = { status: "SYNCED", created: 0, reactivated: 0, aliases_added: 0, deleted: 0, aliases_deleted: 0, subscriptions_deleted: 0, removed: [] };
  if (!inventory) return { ...summary, status: "SKIPPED", delete_skipped_reason: "Apps Script lama belum mengirim inventaris seluruh Sheet1" };
  const accepted = new Set(records.map((record) => normalize(record.data.customer_name)));
  const [stored] = await connection.query("SELECT id, name, is_active FROM datin_institutions FOR UPDATE");
  const [storedAliases] = await connection.query("SELECT institution_id, normalized_name FROM datin_institution_aliases FOR UPDATE");
  const masters = new Map(stored.map((row) => [String(row.id), { ...row, id: String(row.id) }]));
  const byName = new Map(), aliases = new Map();
  for (const row of masters.values()) {
    const name = normalize(row.name);
    if (byName.has(name)) throw fail("Master instansi ambigu setelah normalisasi: " + name, 409);
    byName.set(name, row.id);
  }
  for (const row of storedAliases) {
    const name = normalize(row.normalized_name), id = String(row.institution_id);
    if (!masters.has(id) || (aliases.has(name) && aliases.get(name) !== id)) throw fail("Alias instansi ambigu: " + name, 409);
    aliases.set(name, id);
  }
  const find = (name) => {
    const ids = new Set([byName.get(name), aliases.get(name)].filter(Boolean));
    if (ids.size > 1) throw fail("Nama dan alias menunjuk master berbeda: " + name, 409);
    return [...ids][0];
  };
  const ensureAlias = async (name, id) => {
    const existing = find(name);
    if (existing && existing !== id) throw fail("Alias sudah digunakan instansi lain: " + name, 409);
    if (!aliases.has(name)) {
      await connection.query("INSERT INTO datin_institution_aliases (institution_id, cacti_group_name, normalized_name) VALUES (?, ?, ?)", [id, name, name]);
      aliases.set(name, id);
      summary.aliases_added++;
    }
  };
  const createMaster = async (name) => {
    const [result] = await connection.query("INSERT INTO datin_institutions (name, is_active) VALUES (?, 1)", [name]);
    const id = String(result.insertId);
    if (!/^\d+$/.test(id) || id === "0") throw new Error("ID master instansi baru tidak valid");
    masters.set(id, { id, name, is_active: 1 });
    byName.set(name, id);
    summary.created++;
    return id;
  };
  // Install explicit aliases before resolving sheet names; never use fuzzy matching.
  for (const [short, canonical] of Object.entries(KNOWN_ALIASES)) {
    if (![short, canonical].some((name) => inventory.names.has(name))) continue;
    const shortId = find(short), canonicalId = find(canonical);
    if (shortId && canonicalId && shortId !== canonicalId) throw fail("Master pendek dan kanonis ganda: " + short, 409);
    let id = canonicalId || shortId;
    if (!id && (accepted.has(short) || accepted.has(canonical))) id = await createMaster(canonical);
    if (id) {
      await ensureAlias(short, id);
      await ensureAlias(canonical, id);
    }
  }
  for (const name of accepted) {
    let id = find(name);
    if (!id) id = await createMaster(name);
    const master = masters.get(id);
    if (Number(master.is_active) !== 1) {
      await connection.query("UPDATE datin_institutions SET is_active = 1 WHERE id = ?", [id]);
      master.is_active = 1;
      summary.reactivated++;
    }
    await ensureAlias(normalize(master.name), id);
    await ensureAlias(name, id);
  }
  const retained = new Set([...inventory.names].map(find).filter(Boolean));
  summary.source_rows = inventory.totalRows;
  summary.source_names = inventory.names.size;
  const reasons = [];
  if (!inventory.complete) reasons.push("Snapshot seluruh Sheet1 belum dinyatakan lengkap");
  if (!inventory.names.size || !inventory.totalRows) reasons.push("Inventaris instansi kosong");
  if (inventory.missingNames) reasons.push("Ada baris berisi data tetapi nama instansinya kosong");
  if (!records.length) reasons.push("Tidak ada pelanggan OK yang lolos validasi/SID konflik");
  if ((baseline.guard_rows > 0 && inventory.totalRows <= baseline.guard_rows / 2)
      || (baseline.guard_names > 0 && inventory.names.size <= baseline.guard_names / 2)) reasons.push("Jumlah baris/nama turun 50% atau lebih; perlu pemeriksaan snapshot");
  if (reasons.length) {
    summary.delete_skipped_reason = reasons.join("; ");
    if (baseline.guard_rows > 0) summary.guard_rows = baseline.guard_rows;
    if (baseline.guard_names > 0) summary.guard_names = baseline.guard_names;
    return summary;
  }
  summary.guard_rows = inventory.totalRows;
  summary.guard_names = inventory.names.size;
  const removed = [...masters.values()].filter((row) => !retained.has(row.id));
  for (let offset = 0; offset < removed.length; offset += 250) {
    const chunk = removed.slice(offset, offset + 250), ids = chunk.map((row) => row.id);
    const placeholders = ids.map(() => "?").join(",");
    // Explicit child-first cleanup; never touch Cacti devices, headers or mappings.
    const [subscriptions] = await connection.query(`DELETE FROM telegram_datin_subscriptions WHERE institution_id IN (${placeholders})`, ids);
    const [oldAliases] = await connection.query(`DELETE FROM datin_institution_aliases WHERE institution_id IN (${placeholders})`, ids);
    const [institutions] = await connection.query(`DELETE FROM datin_institutions WHERE id IN (${placeholders})`, ids);
    summary.subscriptions_deleted += subscriptions.affectedRows;
    summary.aliases_deleted += oldAliases.affectedRows;
    summary.deleted += institutions.affectedRows;
    summary.removed.push(...chunk.map(({ id, name }) => ({ id, name })));
  }
  return summary;
}

module.exports = { prepareInstitutionInventory, syncInstitutions };
