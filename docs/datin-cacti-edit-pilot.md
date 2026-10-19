# Uji edit satu device DATIN

`integrations/cacti-agent/edit_device.py` dibatasi pada SID `1165651712`,
SBS host `8471`, DATIN tree `34`, header BANK JATIM `9127`.
Ini bukan editor massal dan belum menjalankan perubahan berkala otomatis.
Tidak membutuhkan SQL baru atau endpoint backend baru: sumber dan mapping
VERIFIED dibaca melalui endpoint agent yang sudah ada. Mapping tidak diubah
karena host, header, server, dan SID tetap sama.

## Alur

1. Ubah kolom SLOT/PORT/ONU di Sheet1 untuk SID tersebut memakai data yang benar,
   tetap STATUS OK, lalu sinkronkan ke HD Work.
2. Jalankan `python3 integrations/cacti-agent/edit_device.py --plan`.
3. Periksa pratinjau `edit-1165651712-plan.report.json` (nilai lama/baru).
4. Jalankan `python3 integrations/cacti-agent/edit_device.py --apply`.
5. Jurnal `edit-1165651712-execution.report.json` menyimpan intent sebelum POST
   dan hasil VERIFIED sesudah read-back.

Sumber harus unik, tidak konflik, valid dan terbaru. Mapping harus VERIFIED
dan cocok persis dengan target pilot. Nilai nol diterima. Kolom SLOT/PORT/ONU
terstruktur adalah sumber edit; script tidak menafsirkan ulang kolom FSP.
Jika FSP juga digunakan proses lain, tim data perlu merapikannya agar konsisten.

Script hanya mengubah input `slot`, `port`, `onu_id`. IP OLT, serial number,
description, opsi monitoring dan pengaturan lain dipertahankan. Perubahan IP,
SN atau description pada sumber ditolak untuk uji ini. Tidak membuat/menghapus
device, tidak mengubah tree/header, dan tidak menyentuh host eksternal atau ATCS.
Host/tree/form dan mapping diperiksa ulang sebelum write; opsi lain diikat hash
untuk mendeteksi perubahan manual sejak pratinjau. Notes dan monitor_text adalah
data runtime poller sehingga tidak dijadikan penyebab konflik fingerprint.
Form disubmit dengan nilai opsi runtime terbaru saat dibaca.

Jangan jalankan editor manual/agent lain pada host yang sama bersamaan.
Cacti tidak menyediakan compare-and-swap untuk form legacy ini; pemeriksaan
optimistis tidak merupakan kunci global terhadap perubahan manual di server.
Lock lokal yang sama dengan agent bulk/header mencegah proses tumpang tindih
di komputer agent ini, bukan di semua komputer.

## Jika timeout atau hasil belum pasti

`python3 integrations/cacti-agent/edit_device.py --resume` hanya membaca dan
merekonsiliasi hasil pada host yang sama. **Tidak pernah mengulang POST edit**.
Jika nilai Cacti belum sama dengan target, script berhenti untuk pemeriksaan.
Tidak rollback otomatis karena dapat menimpa perubahan pihak lain.

Jurnal ditulis atomik, privat 0600, tanpa CSRF/password/SNMP community. Opsi
non-target diikat dengan hash, bukan disimpan sebagai plaintext. Jangan hapus
jurnal untuk memaksa pengulangan. Untuk uji lanjutan yang disetujui, gunakan
pasangan `--report` dan `--journal` baru dan buat pratinjau baru; jangan memakai
ulang rencana/jurnal yang telah selesai. File `.report.json` sudah diabaikan Git.

## Hasil uji awal

Atas permintaan pengguna setelah sumber spreadsheet diubah, script berhasil
memperbarui slot `8 → 9`, port `6 → 7`, ONU tetap `19`. Read-back memverifikasi
konfigurasi target serta opsi non-target, host `8471` dan header `9127` tetap.
Ini membuktikan jalur edit form, bukan bahwa nilai uji tersebut mewakili lokasi
ONT fisik atau status ONT sudah ONLINE. Collector/alarm berjalan normal dan
akan mengikuti hasil monitoring Cacti dari lokasi yang kini dikonfigurasi.
