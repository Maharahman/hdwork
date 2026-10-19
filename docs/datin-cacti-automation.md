# Sinkronisasi DATIN berkala: Spreadsheet → HD Work → Cacti

Scope: header baru, create device, edit host yang sama, delete atas NOT eksplisit,
dan pembaruan database dengan audit. NODEB, ATCS dan host eksternal tidak dikelola.
Ini menggantikan executor pilot/bulk manual untuk operasi rutin DATIN.

## Alur dan database

1. Apps Script mengirim snapshot Sheet1 setiap 10 menit. `rows` tetap hanya OK,
   tetapi `device_inventory` memuat SID/status/server/instansi/nomor baris dari
   **semua** baris non-kosong, termasuk NOT. Snapshot diproses atomik.
2. Backend memeriksa kesesuaian inventaris lengkap dengan baris OK/instansi.
   Duplikasi SID lintas OK/NOT atau beda kapital ditahan seluruhnya. Nol tetap
   nilai valid pada slot/port/ONU; SID tetap teks.
3. Master diperbarui untuk OK. Master historis yang menjadi NOT diberi
   `source_status = NOT`; konflik menjadi CONFLICT. Metadata lengkap disimpan
   pada `datin_sheet_sources.device_inventory_json` beserta timestamp.
4. Agent di jaringan VPN menjalankan siklus tunggal tanpa overlap, lalu menunggu
   600 detik sesudah selesai sebelum siklus berikutnya. Snapshot baru yang isinya
   identik tidak membatalkan seluruh pekerjaan; kelayakan setiap SID diperiksa
   ulang secara semantik sebelum write.
5. Run/job direservasi di database sebelum write. Hanya satu run aktif per sumber.
   Token berupa hash di server; token asli dan intent disimpan dalam jurnal lokal
   atomik privat 0600. Read-back wajib sebelum job VERIFIED.
6. Mapping mencatat `last_applied_json`, timestamp sumber, ID job, dan lifecycle
   ACTIVE/DELETED. Edit tidak mengganti host ID. NOT yang terverifikasi terhapus
   mengubah mapping menjadi DELETED; bila sumber masih NOT unik, master pelanggan
   diarsipkan ke `datin_customer_archives`, lalu dihapus. Riwayat tidak dipurge.
7. Host yang sudah dihapus manual (pilot SBS 8471) cukup diverifikasi tidak ada,
   lalu direkonsiliasi ke database, tanpa delete ulang. NOT kembali OK mengizinkan
   create generasi baru hanya setelah bukti tidak ada host `[HD-D]` untuk SID itu.
   Host baru bisa memiliki ID berbeda; mapping/konfigurasi lama ada dalam audit.

Collector tetap proses terpisah dan tidak perlu diubah. Ia membaca Cacti aktual
dan mengirim down/health ke backend produksi; routing Telegram tidak berubah.
Alarm historis mengikuti scan collector berikutnya, bukan ditandai ONLINE buatan.

## Pengaman

- SID hilang, status kosong/selain OK/NOT, konflik, snapshot parsial atau kedaluwarsa
  bukan perintah delete. Apps Script lama tidak dapat mengotorisasi reconcile.
- Penurunan jumlah baris bisnis 50% atau lebih menahan delete. Baris kosong/formating
  tidak dihitung sebagai data. Upgrade pertama memakai master historis sebagai
  baseline bila belum ada inventaris lama.
- Default lebih dari 10 kandidat delete menahan **seluruh** delete pada siklus itu,
  bukan menghapus sepuluh pertama. Periksa sumber sebelum menaikkan batas (maks100).
- Edit/delete hanya untuk mapping VERIFIED dan prefix/SID yang sesuai. Kandidat
  bertanda tanpa mapping aktif tidak diadopsi. Device eksternal tidak disentuh.
- Edit mendukung description, IP OLT, slot, port, ONU, dan SN; opsi form lain
  dipertahankan. Kolom terstruktur dipakai, bukan menafsirkan ulang FSP.
- **Perubahan server/tree/header instansi pada mapping aktif ditahan untuk review
  migrasi manual**, bukan memindah host atau membuat duplikat otomatis.
- Host yang digunakan tree lain tidak diedit/dihapus. Audit tree mencakup tree
  yang terlihat akun agent dan harus konsisten dengan menu Devices, bukan klaim
  akses ke tree tersembunyi oleh permission.
- Tidak pernah menghapus header, termasuk header custom/kosong/ATCS. Delete host
  memilih preserve graph/data sources; Cacti dapat menonaktifkan data sources
  terkait, tetapi tidak diminta menghapus graph/RRD.
- Cache dari receipt VERIFIED dipakai saat konfigurasi belum berubah. Nama dan
  membership DATIN tetap diperiksa lewat inventaris. Perubahan slot/opsi manual
  langsung di Cacti tidak selalu terdeteksi cache: gunakan `--full-audit` untuk
  membaca seluruh host. Jangan menjalankan editor manual bersamaan.
- Tidak ada transaksi atomik lintas spreadsheet/database/Cacti. Hasil write yang
  sudah terjadi tetap dicatat jika sumber berubah, lalu siklus berikutnya memakai
  sumber baru. NOT kembali OK saat delete tidak menghapus master OK yang baru.

## Aktivasi sekali (sesudah push/pull)

Fitur **belum aktif hanya dengan pull**. Perlu SQL, backend, Apps Script, flag,
dan proses agent. Tidak ada dependensi baru atau build frontend.

### 1. Server backend

Backup `hd_work` dahulu. Dari `/root/HD/hdwork`, setelah migrasi DATIN lama terpasang:

```bash
mysql -u root -p hd_work < database/migrations/20261019_add_datin_cacti_reconcile.sql
pm2 restart hdwork-backend
```

SQL dijalankan sekali. Biarkan flag otomatisasi belum aktif saat pratinjau awal.

### 2. Apps Script

Ganti kode dengan `integrations/google-apps-script/datin-sync.gs`, simpan, lalu
**HD Work → Sinkronkan sekarang**. Pastikan `Inventaris SID: COMPLETE`. Script
Properties/key/URL dan trigger 10 menit lama tetap dipakai, tidak perlu dibuat ulang.
NOT/konflik ikut sebagai metadata, bukan pelanggan aktif.

### 3. Pratinjau dari komputer VPN

Pakai `.env` agent lama. Komputer harus menyala, tidak sleep, dan VPN menjangkau
SBS/SBU. Jangan pindah agent ke container backend kecuali punya akses VPN juga.

```bash
cd /Users/fajarkhojek/hd_work
/usr/local/bin/python3 integrations/cacti-agent/reconcile.py --plan
```

Periksa `reconcile-plan.report.json`. Laporan ini observasi; eksekusi selalu memakai
data terbaru, bukan mengeksekusi laporan lama secara buta. Opsional `--plan --full-audit`.
VERIFY berarti konfigurasi live cocok dan cache database dapat disegarkan, bukan edit.

### 4. Aktifkan dan jalankan satu siklus

Tambahkan pada `.env` **backend produksi**, bukan Apps Script:

```dotenv
DATIN_CACTI_AUTOMATION_ENABLED=1
DATIN_CACTI_MAX_DELETES_PER_RUN=10
```

Restart `pm2 restart hdwork-backend`. Writer API pilot/bulk/header lama ditolak
saat flag aktif. Jangan jalankan script manual lama dari komputer lain: Cacti
sendiri tidak memiliki kunci global terhadap operator manual.

Di komputer VPN:

```bash
/usr/local/bin/python3 integrations/cacti-agent/reconcile.py --apply
```

Periksa log/database. Run selesai diarsipkan sebagai `reconcile-UUID.report.json`.

### 5. Jalankan berkala setelah satu siklus berhasil

```bash
/usr/local/bin/python3 integrations/cacti-agent/reconcile.py --watch --apply --interval 600
```

Atau jika PM2 tersedia pada komputer VPN:

```bash
pm2 start integrations/cacti-agent/ecosystem.config.cjs
pm2 logs datin-cacti-sync --lines 60
pm2 save
```

Config memakai `/usr/local/bin/python3`; sesuaikan lewat `PYTHON_BIN` bila perlu.
Collector tetap proses tersendiri. Jangan membuat dua proses sync. PM2 online
saja bukan bukti berhasil: periksa log dan run DONE.

## Pemulihan dan audit

Restart proses atau jalankan `--apply` dengan mesin, `.env` dan jurnal aktif yang
sama. Run/job lama dipulihkan dahulu. ACK backend boleh di-replay. Job yang belum
memiliki intent mutasi Cacti dapat dibatalkan bila sumber berubah. Setelah mutasi
mungkin terjadi, baca ulang hasil lama, jangan mengulang POST.

Jika hasil tetap ambigu (create belum unik, edit belum cocok setelah timeout,
delete pernah dicoba tetapi host ada, opsi manual/tree berubah), run ditahan.
Siklus berikutnya mencoba rekonsiliasi, bukan membuat ulang. Tidak ada expiry
otomatis yang melepas reservasi lalu menciptakan duplikat. Butuh review operator
untuk keadaan yang tidak dapat dipastikan.

Jangan hapus jurnal/reservasi atau mengganti token untuk memaksa pengulangan.
Jurnal memuat token, privat 0600, tidak boleh diunggah. `.report.json`, `.env` dan
log agent diabaikan Git. Pindah mesin harus menghentikan proses lama dahulu dan
memindahkan jurnal secara privat, bukan menjalankan dua salinan. Jangan mengarsipkan
semua `.report.json` dengan glob ketika ada run aktif.

Hentikan melalui `pm2 stop datin-cacti-sync` atau Ctrl+C. Flag backend `0` menjadi
pengaman tambahan; rekonsiliasi job yang pernah mencoba write dahulu sebelum
kembali memakai writer manual.

```sql
SELECT id, run_status, heartbeat_at, completed_at, last_error
FROM datin_cacti_sync_runs ORDER BY created_at DESC LIMIT 10;
SELECT sid, operation, job_status, created_at, attempted_at, verified_at
FROM datin_cacti_sync_jobs ORDER BY created_at DESC LIMIT 30;
SELECT sid, host_id, sync_state, last_source_snapshot_at, deleted_at
FROM datin_cacti_devices ORDER BY updated_at DESC LIMIT 30;
SELECT sid, reason, snapshot_at, archived_at
FROM datin_customer_archives ORDER BY archived_at DESC LIMIT 30;
```

`last_error` dapat menyimpan kegagalan historis walau run akhirnya DONE; lihat
status dan timestamp. Unit/integration tests tidak menyentuh Cacti produksi.
Uji SQL opsional menggunakan socket MySQL sementara `/tmp/hdwork-reconcile-mysql.*`
dan skema sintetis yang dihapus sesudah tes, tanpa membaca `.env` database pengguna.
