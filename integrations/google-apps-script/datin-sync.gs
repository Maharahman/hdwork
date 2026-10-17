// Paste into the Apps Script project bound to the source spreadsheet.
const DATIN_SPREADSHEET_ID = '16tSELEZFzbH7FdI3NFqnNJx17xVf8GXgvZD5ghfxkLE';
const DATIN_COLUMNS = {
  sid: 'SID', service: 'SERVICE', customer_category: 'KAT. PLGN',
  customer_name: 'NAME_CUSTOMER', customer_detail: 'CUST_DETAIL', address: 'ADDRESS',
  access_node: 'NODE_AKSES', fsp: 'FSP', serial_number: 'SN ONT / L2SW',
  slot: 'SLOT', port: 'PORT', onu: 'ONU', olt_ip: 'IP OLT', server: 'SERVER', status: 'STATUS'
};

function onOpen() {
  SpreadsheetApp.getUi().createMenu('HD Work')
    .addItem('Sinkronkan sekarang', 'syncDatinNow')
    .addItem('Lihat hasil terakhir', 'showLastDatinSync')
    .addSeparator()
    .addItem('Aktifkan jadwal 10 menit', 'installDatinSyncTrigger')
    .addToUi();
}

function readDatinRows_(grid) {
  if (!grid.length) throw new Error('Sheet1 tidak memiliki header.');
  const headers = grid[0].map(function (value) { return String(value).trim().toUpperCase().replace(/\s+/g, ' '); });
  const columns = {};
  Object.keys(DATIN_COLUMNS).forEach(function (key) {
    const header = DATIN_COLUMNS[key];
    const index = headers.indexOf(header);
    if (index < 0 || headers.lastIndexOf(header) !== index) throw new Error('Header hilang atau ganda: ' + header);
    columns[key] = index;
  });
  const rows = [];
  grid.slice(1).forEach(function (values, index) {
    if (String(values[columns.status] || '').trim().toUpperCase() !== 'OK') return;
    const row = { source_row: index + 2 };
    Object.keys(columns).forEach(function (key) {
      row[key] = String(values[columns[key]] == null ? '' : values[columns[key]]).trim();
    });
    rows.push(row);
  });
  return rows;
}

function runDatinSync_() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) throw new Error('Sinkronisasi lain sedang berjalan. Coba lagi nanti.');
  const properties = PropertiesService.getScriptProperties();
  try {
    const url = properties.getProperty('HDWORK_SYNC_URL');
    const key = properties.getProperty('HDWORK_SYNC_KEY');
    if (!url || !/^https:\/\//i.test(url) || !key) throw new Error('Isi HDWORK_SYNC_URL (HTTPS) dan HDWORK_SYNC_KEY di Script Properties.');
    const spreadsheet = SpreadsheetApp.openById(DATIN_SPREADSHEET_ID);
    const sheet = spreadsheet.getSheetByName('Sheet1');
    if (!sheet) throw new Error('Tab Sheet1 tidak ditemukan.');
    const snapshotAt = new Date().toISOString();
    const rows = readDatinRows_(sheet.getDataRange().getDisplayValues());
    const payload = JSON.stringify({
      spreadsheet_id: spreadsheet.getId(), sheet_name: sheet.getName(),
      run_id: Utilities.getUuid(), snapshot_at: snapshotAt, rows: rows
    });
    let result;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      let response;
      try {
        response = UrlFetchApp.fetch(url, {
          method: 'post', contentType: 'application/json',
          headers: { 'X-Sheet-Sync-Key': key }, payload: payload,
          muteHttpExceptions: true, followRedirects: false
        });
      } catch (error) {
        if (attempt === 2) throw new Error('Koneksi ke HD Work gagal. Data server sebelumnya tetap disimpan.');
        Utilities.sleep((attempt + 1) * 3000);
        continue;
      }
      const code = response.getResponseCode();
      if ((code === 429 || code >= 500) && attempt < 2) {
        Utilities.sleep((attempt + 1) * 3000);
        continue;
      }
      try { result = JSON.parse(response.getContentText()); }
      catch (error) { throw new Error('HTTP ' + code + ': respons server bukan JSON. Periksa alamat backend/proxy.'); }
      if (code < 200 || code >= 300 || !result.success) throw new Error('HTTP ' + code + ': ' + (result.message || 'Sinkronisasi gagal.'));
      break;
    }
    const summary = result.summary;
    const lines = [
      'Selesai: ' + new Date().toISOString(),
      'Baris OK dikirim: ' + summary.received,
      'Baru: ' + summary.created + ' · Diperbarui: ' + summary.updated + ' · Tetap: ' + summary.unchanged,
      'SID konflik: ' + summary.conflicts.length + ' · Baris tidak valid: ' + summary.invalid_rows.length
    ];
    summary.conflicts.slice(0, 15).forEach(function (conflict) {
      lines.push('SID ' + conflict.sid + ': baris ' + conflict.rows.join(', '));
    });
    summary.invalid_rows.slice(0, 15).forEach(function (row) { lines.push('Baris ' + row.row + ': ' + row.reason); });
    if (summary.conflicts.length > 15 || summary.invalid_rows.length > 15) lines.push('Rincian lengkap tersimpan di database datin_sheet_sync_runs.');
    const report = lines.join('\n').slice(0, 7000);
    properties.setProperty('HDWORK_LAST_SYNC', report);
    properties.deleteProperty('HDWORK_LAST_ERROR');
    console.log(report);
    return report;
  } catch (error) {
    properties.setProperty('HDWORK_LAST_ERROR', new Date().toISOString() + ': ' + error.message);
    throw error;
  } finally {
    lock.releaseLock();
  }
}

function syncDatinNow() {
  try { SpreadsheetApp.getUi().alert('Sinkronisasi HD Work', runDatinSync_(), SpreadsheetApp.getUi().ButtonSet.OK); }
  catch (error) { SpreadsheetApp.getUi().alert('Sinkronisasi gagal: ' + error.message); }
}

function syncDatinScheduled() {
  // Throw errors to Apps Script execution history; no UI is available to scheduled jobs.
  runDatinSync_();
}

function showLastDatinSync() {
  const properties = PropertiesService.getScriptProperties();
  SpreadsheetApp.getUi().alert([
    properties.getProperty('HDWORK_LAST_SYNC') || 'Belum ada sinkronisasi berhasil.',
    properties.getProperty('HDWORK_LAST_ERROR') ? 'Error terakhir: ' + properties.getProperty('HDWORK_LAST_ERROR') : ''
  ].filter(Boolean).join('\n\n'));
}

function installDatinSyncTrigger() {
  const lock = LockService.getScriptLock();
  lock.waitLock(5000);
  try {
    const properties = PropertiesService.getScriptProperties();
    if (!properties.getProperty('HDWORK_SYNC_URL') || !properties.getProperty('HDWORK_SYNC_KEY')) throw new Error('Isi Script Properties sebelum mengaktifkan jadwal.');
    // Only manage this function's triggers belonging to the current user.
    ScriptApp.getProjectTriggers().filter(function (trigger) {
      return trigger.getHandlerFunction() === 'syncDatinScheduled';
    }).forEach(function (trigger) { ScriptApp.deleteTrigger(trigger); });
    ScriptApp.newTrigger('syncDatinScheduled').timeBased().everyMinutes(10).create();
    SpreadsheetApp.getUi().alert('Jadwal sinkronisasi 10 menit sudah aktif untuk akun ini.');
  } finally {
    lock.releaseLock();
  }
}
