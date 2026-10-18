# Pratinjau pembuatan device DATIN

Tahap ini hanya membaca dan menghasilkan rencana. Tidak ada `--apply`, POST ke
host.php, simpan mapping device, tambah host, edit host, hapus host, atau perubahan
tree otomatis. Plan bukan daftar yang boleh langsung dieksekusi.

## Deploy persiapan

1. Deploy backend terbaru seperti biasa.
2. Jalankan migrasi `database/migrations/20261019_add_datin_cacti_devices.sql`
   pada database HD Work setelah migrasi spreadsheet dan header sebelumnya.
   Migrasi membuat tabel kosong, tanpa mengambil alih device eksternal.
3. Restart backend. Endpoint GET `/api/datin-cacti/device-mappings` menggunakan
   `X-Cacti-Agent-Key`, sama dengan endpoint agent lainnya.

Mapping menyimpan spreadsheet ID, SID asli, server, tree ID, host ID, header ID,
status kepemilikan, dan waktu verifikasi. SID unik per sumber; host ID unik per
server. Status awal REVIEW_REQUIRED. Prefix saja tidak menghasilkan VERIFIED.
Tahap ini sengaja tidak menyediakan endpoint penulisan mapping; proses konfirmasi
dan penyimpanan mapping terverifikasi disiapkan bersama tahap pembuatan device.
Jangan isi mapping lewat SQL hanya untuk melewati HOLD.

## Jalankan pratinjau dari komputer ber-VPN

```bash
/usr/local/bin/python3 integrations/cacti-agent/compare_devices.py --plan
```

Menggunakan konfigurasi `.env` agent yang sudah ada. Tanpa `--plan`, compare lama
tetap dapat berjalan tanpa tabel mapping baru. Dengan `--plan`, endpoint atau
migrasi yang belum tersedia menyebabkan proses berhenti, bukan dianggap mapping kosong.

CSV/JSON pada `comparison.report.*` diperbarui. JSON berisi `device_plan` lengkap,
CSV menambahkan `planned_actions`, `plan_reasons`, dan `planned_header_id`.
Gunakan `--report-prefix /path/datin-plan.report` bila ingin memisahkan laporan.
Kolom tindakan dapat berulang untuk SID dengan beberapa kandidat; ringkasan JSON
menghitung tindakan per SID, bukan jumlah baris CSV. CREATE_HEADER per SID adalah
dependensi; bukan jumlah header unik yang harus dibuat.

| Tindakan | Makna |
| --- | --- |
| CREATE_HEADER | Instansi belum mempunyai header pada server tujuan |
| CREATE_DEVICE | Rencana host baru khusus DATIN dengan prefix `[HD-D]` |
| ATTACH_TO_TREE | Rencana menautkan host DATIN ke header tujuan |
| UPDATE_DEVICE | Mapping VERIFIED ada dan konfigurasi/nama berbeda |
| NO_CHANGE | Mapping VERIFIED dan data yang diperiksa sesuai |
| HOLD | Konflik, validasi gagal, kandidat belum terpetakan, mapping/identitas bermasalah, atau snapshot berubah |

Untuk EXTERNAL_ONLY, rencana boleh memuat CREATE_DEVICE yang terpisah. Host
eksternal hanya tercantum sebagai informasi, tidak menjadi host target. ATCS dan
header monitoring khusus tetap dilindungi. Nilai slot/port/ONU 0 valid, tetapi
null/kosong tidak valid; STATUS wajib OK.

Header dibaca langsung dari tree DATIN SBS 34 dan SBU 28. Header duplikat ditahan.
Mapping host hilang, prefix/SID berubah, atau server/tree berpindah ditahan, tidak
menjadi CREATE pengganti. Kandidat `[HD-D]` yang belum mempunyai mapping juga HOLD.
Perbedaan keanggotaan header ditahan karena host mungkin dipakai tree lain.
Keanggotaan tree lain belum diinventarisasi; pratinjau ini bukan izin mengubah
host bersama atau menghapus apa pun. Sumber dan mapping dibaca ulang setelah scan;
perubahan selama scan menahan seluruh rencana. Rencana adalah snapshot bertanggal,
bukan antrean eksekusi yang boleh dipakai ulang tanpa inventaris baru.

## Sebelum tahap pembuatan

Tinjau beberapa SID di tiap server beserta form device, opsi template/monitor,
batas panjang Description, dan hasil konfigurasi yang diinginkan. Lalu lakukan
uji create terpisah setelah persetujuan. Pembuatan harus membaca kembali host ID,
menyimpan mapping terverifikasi, mencegah worker paralel, dan tidak mengulang POST
secara buta bila timeout. Collector tetap terpisah dan belum diubah oleh tahap ini.
