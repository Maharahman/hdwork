# Sinkronisasi master DATIN dari Google Spreadsheet

Sumber: `16tSELEZFzbH7FdI3NFqnNJx17xVf8GXgvZD5ghfxkLE`, tab `Sheet1`.
Apps Script mengirim snapshot baris `STATUS = OK` melalui HTTPS setiap 10 menit.
Jadwal Google bersifat berkala, tidak harus tepat pada menit 00/10/20.
Menu `HD Work → Sinkronkan sekarang` tersedia saat spreadsheet dibuka ulang.

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

```sql
SELECT sid, customer_name, source_row, last_synced_at
FROM datin_customer_master ORDER BY last_synced_at DESC LIMIT 20;

SELECT run_id, created_at, result_json
FROM datin_sheet_sync_runs ORDER BY created_at DESC LIMIT 10;
```

Dokumentasi Google: [time-driven triggers](https://developers.google.com/apps-script/guides/triggers/installable), [jadwal everyMinutes](https://developers.google.com/apps-script/reference/script/clock-trigger-builder), [UrlFetchApp](https://developers.google.com/apps-script/reference/url-fetch/url-fetch-app), [Script Properties](https://developers.google.com/apps-script/reference/properties/properties-service).
