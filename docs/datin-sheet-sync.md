# Sinkronisasi master DATIN dari Google Spreadsheet

Sumber: `16tSELEZFzbH7FdI3NFqnNJx17xVf8GXgvZD5ghfxkLE`, tab `Sheet1`.
Apps Script mengirim snapshot baris `STATUS = OK` melalui HTTPS setiap 10 menit.
Versi terbaru juga mengirim inventaris nama instansi dari seluruh Sheet1 untuk
memperbarui pilihan `/alarmdatin` dan membersihkan master yang benar-benar hilang.
Jadwal Google bersifat berkala, tidak harus tepat pada menit 00/10/20.
Menu `HD Work → Sinkronkan sekarang` tersedia saat spreadsheet dibuka ulang.

Untuk rekonsiliasi device Cacti berkala, versi terbaru juga mengirim inventaris
SID/status lengkap (termasuk NOT) secara terpisah dari data pelanggan OK.
Panduan aktivasi, migrasi dan pengamannya: [datin-cacti-automation.md](datin-cacti-automation.md).

## 1. Backend

- Jalankan `database/migrations/20261018_add_datin_sheet_sync.sql` sekali pada database yang dipakai backend.
- Buat key khusus, misalnya dengan `openssl rand -hex 32`, dan tambahkan ke `.env` server:

```env
DATIN_SHEET_SYNC_API_KEY=isi_key_khusus_di_sini
DATIN_SHEET_ID=16tSELEZFzbH7FdI3NFqnNJx17xVf8GXgvZD5ghfxkLE
```

- Restart proses backend aktif. Endpoint: `POST /api/datin-sheet/sync`.
- Pastikan proxy menerima JSON sampai 5 MB di endpoint ini.

## 2. Apps Script

1. Di spreadsheet sumber, buka **Extensions → Apps Script**.
2. Salin kode `integrations/google-apps-script/datin-sync.gs` ke editor. Jika proyek sudah memiliki `onOpen`, gabungkan penambahan menu ke fungsi tersebut, jangan hapus fungsi lain.
3. Pada **Project Settings → Script Properties**, tambahkan:

| Property | Nilai |
| --- | --- |
| `HDWORK_SYNC_URL` | `https://serverhdwork.telkomakses-area3.id/api/datin-sheet/sync` |
| `HDWORK_SYNC_KEY` | Key yang sama dengan `DATIN_SHEET_SYNC_API_KEY` pada backend |

4. Simpan script dan buka ulang spreadsheet. Pilih **HD Work → Sinkronkan sekarang**, lalu berikan izin Google saat diminta. Tidak perlu deploy sebagai web app.
5. Periksa ringkasan sinkron manual. Setelah berhasil, pilih **HD Work → Aktifkan jadwal 10 menit** menggunakan satu akun penanggung jawab saja. Trigger berjalan dengan akun pembuatnya dan memerlukan akses spreadsheet yang tetap aktif.
6. Lihat hasil dari menu **Lihat hasil terakhir**, halaman **Executions** Apps Script, atau tabel `datin_sheet_sync_runs` di server.

SID harus disimpan sebagai teks biasa (Plain text) di sumber. Script memakai nilai tampilan supaya format SID tidak berubah menjadi angka JavaScript; SID yang sudah kehilangan angka nol di sumber tidak dapat dipulihkan otomatis.

## Aturan data

- Header dicocokkan berdasarkan nama; urutan kolom boleh berubah. Semua kolom pada `DATIN_COLUMNS` wajib ada dan tidak ganda. Kolom lain diabaikan.
- Hanya baris `OK` dikirim, tanpa membedakan huruf besar/kecil atau spasi tepi.
- SID menjadi kunci unik per spreadsheet. Semua kemunculan SID duplikat pada baris OK ditahan, termasuk duplikat yang identik. Baris OK lainnya diproses.
- Nama pelanggan atau SID kosong dilaporkan dan dilewati. SID dan seluruh field dikirim sebagai teks.
- Snapshot diproses dalam transaksi. Error database membatalkan seluruh transaksi. Retry memakai run ID yang sama sehingga hasil commit yang sudah berhasil tidak diproses dua kali.
- Snapshot yang lebih tua daripada snapshot terakhir ditolak agar tidak menimpa update baru.
- Data yang tidak muncul, menjadi NOT, atau konflik tetap dipertahankan di database. Tidak ada penghapusan otomatis. `last_synced_at` menunjukkan kapan setiap SID terakhir diterima valid.
- `STATUS` adalah status validasi spreadsheet, bukan status ONLINE/DOWN dari Cacti.
- Master ini disimpan terpisah pada `datin_customer_master`; sinkron tidak menulis ke `cacti_alarms`, mengubah Cacti, atau mengirim Telegram.
- Akses Google hanya diperlukan di Apps Script. Server tidak membutuhkan konektor Google Drive atau service account.

## Sinkronisasi pilihan instansi Telegram (mode pengembangan)

Data pelanggan `datin_customer_master` tetap mengikuti aturan di atas: hanya OK
valid yang diperbarui, data lama yang ditahan tidak dihapus. Aturan pembersihan
berikut hanya untuk `datin_institutions`, alias, dan pilihan subscriber.

- Instansi baru dibuat dari nama unik pada baris OK yang lolos validasi/SID konflik.
- Nama dicocokkan memakai normalisasi case/spasi dan alias eksplisit, bukan fuzzy.
  ID master yang masih sesuai dipertahankan agar pilihan user tidak hilang.
- `BANK CENTRAL ASIA` diberi alias ke master `BANK CENTRAL ASIA TBK (BANK BCA)`;
  `BANK DANAMON` ke `BANK DANAMON INDONESIA TBK`. Tidak membuat master pendek ganda.
- Nama baru seperti `DISKOMINFO KAB SUMENEP` menjadi master tersendiri. User tetap
  harus memilihnya; sinkron tidak mendaftarkan user ke instansi baru otomatis.
- Inventaris `institution_inventory` memuat nama dan status dari seluruh Sheet1,
  termasuk NOT dan SID konflik. Nama tersebut melindungi master lama dari
  penghapusan, tetapi tidak membuat master baru bila hanya NOT/konflik.
- Setelah snapshot lengkap/nonkosong yang konsisten, master yang tidak lagi
  disebut oleh nama maupun alias di seluruh Sheet1 dihapus. Pilihan subscriber
  dan alias terkait dihapus lebih dulu dalam transaksi yang sama.
- Ini mencakup master awal/manual yang sudah tidak ada di spreadsheet, sesuai
  keputusan masa pengembangan. Tidak menghapus akun subscriber Telegram, master
  pelanggan, alarm, device/header Cacti, atau mapping device/header.
- Nama yang berubah tanpa alias dianggap instansi baru dan nama lama dapat
  dibersihkan. Tambahkan alias eksplisit sebelum mengganti nama jika ID/pilihan
  lama harus dipertahankan. Master/alias ambigu ditolak, bukan digabung diam-diam.
- Pembersihan ditahan bila inventaris tidak lengkap/kosong, ada baris berisi data
  tanpa nama, tidak ada OK yang lolos, atau jumlah baris/nama turun 50% atau lebih
  dari baseline terakhir yang terpercaya. Baseline tidak diturunkan saat ditahan.
  Periksa spreadsheet dan ringkasan sebelum mempertimbangkan penyesuaian manual;
  jangan mengubah riwayat audit untuk memaksa penghapusan.
- Inventaris salah format/tidak cocok dengan baris OK ditolak sebelum penulisan.
  Kegagalan database membatalkan seluruh snapshot termasuk pelanggan/instansi.
- Ringkasan `summary.institutions` menyimpan jumlah dibuat, alias ditambah,
  master/alias/pilihan subscriber dihapus, daftar ID/nama yang dihapus, serta
  alasan pembersihan ditahan. Retry run ID yang sama tidak menghapus ulang.
- Tombol Telegram lama untuk instansi yang sudah dihapus akan memuat ulang daftar,
  bukan membuat langganan ke ID yang tidak ada. Halaman juga dijepit ke rentang baru.

### Cara mengaktifkan update ini

1. Deploy backend terbaru dan restart proses backend. Tidak ada migrasi SQL baru;
   tabel master instansi, alias, dan subscriptions dari migrasi DATIN sebelumnya
   harus sudah tersedia.
2. Ganti kode Apps Script dengan versi terbaru `integrations/google-apps-script/datin-sync.gs`.
   Properties URL/key dan trigger 10 menit yang sudah ada tidak perlu dibuat ulang.
3. Jalankan **HD Work → Sinkronkan sekarang** sekali dan periksa ringkasannya.
   Sinkron pertama dapat menghapus master awal yang sudah tidak ada di Sheet1
   beserta pilihan subscriber terkait. Backup bila pilihan lama masih dibutuhkan.
4. Kirim ulang `/alarmdatin` untuk memuat daftar terbaru; pengguna tidak perlu
   register ulang akun atau kehilangan pilihan untuk instansi yang ID-nya bertahan.

Backend terbaru dengan Apps Script lama tetap menyinkronkan pelanggan, tetapi
sinkron instansi berstatus SKIPPED dan tidak membersihkan apa pun. Memasang backend
saja belum mengaktifkan fitur ini. Pengiriman seluruh alarm DATIN ke grup tetap
sama; alias memengaruhi pencocokan instansi untuk filter japri.

```sql
SELECT sid, customer_name, source_row, last_synced_at
FROM datin_customer_master ORDER BY last_synced_at DESC LIMIT 20;

SELECT run_id, created_at, result_json
FROM datin_sheet_sync_runs ORDER BY created_at DESC LIMIT 10;
```

Dokumentasi Google: [time-driven triggers](https://developers.google.com/apps-script/guides/triggers/installable), [jadwal everyMinutes](https://developers.google.com/apps-script/reference/script/clock-trigger-builder), [UrlFetchApp](https://developers.google.com/apps-script/reference/url-fetch/url-fetch-app), [Script Properties](https://developers.google.com/apps-script/reference/properties/properties-service).
