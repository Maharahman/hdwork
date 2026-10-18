# Pratinjau pencocokan spreadsheet dan device Cacti

`integrations/cacti-agent/compare_devices.py` hanya membaca. Tidak ada opsi apply, perubahan kepemilikan, atau aksi tambah/edit/hapus device/header. Report CSV menampilkan data sumber dan Cacti berdampingan; JSON memuat rincian kandidat, device tidak terpetakan dan ringkasan.

## Persiapan

- Deploy backend beserta endpoint agent dan jalankan migrasi header pada `docs/datin-cacti-headers.md`.
- Jalankan sinkron spreadsheet berikutnya agar `valid_snapshot_at` terisi. Agent hanya mengambil baris yang lolos snapshot terakhir melalui `GET /api/datin-cacti/device-source`.
- Isi `.env` agent yang sama seperti agent header: key agent HD Work dan username/password Cacti SBS/SBU.
- Jalankan pada komputer yang terhubung VPN. Dependency sama dengan `sync_headers.py`.

```bash
cd /path/ke/hdwork/integrations/cacti-agent
.venv/bin/python compare_devices.py
```

Output: `comparison.report.json` dan `comparison.report.csv`. Untuk jalur lain:

```bash
.venv/bin/python compare_devices.py --report-prefix /path/laporan/datin.report
```

## Cara membaca hasil

| Status | Arti |
| --- | --- |
| MATCH | SID satu kandidat pada server tujuan, lokasi dan header sesuai |
| LOCATION_DIFF | IP OLT/slot/port/ONU/SN berbeda; lihat kolom sebelum/sesudah |
| HEADER_DIFF | Lokasi sama tetapi header DATIN tidak sesuai, belum terpasang, atau keanggotaan ganda |
| OTHER_SERVER | SID hanya ditemukan pada server lain |
| NOT_FOUND | SID tidak ditemukan pada nama seluruh device; tetap perlu pemeriksaan sebelum membuat device |
| AMBIGUOUS | SID ganda pada sumber/target atau ditemukan di kedua server |
| INCOMPLETE | Kolom wajib, status atau server sumber tidak valid |
| READ_ERROR | Detail host tidak berhasil dibaca atau identitas berubah saat scan |
| SOURCE_CONFLICT | SID konflik pada snapshot; semua baris SID tersebut ditahan |
| PROTECTED | Header ATCS atau nama perangkat berawalan ATCS; dikelola manual |

JSON bagian `unmanaged` berisi device yang tidak cocok dengan SID sumber. `UNMAPPED` bukan instruksi menghapus atau bukti perangkat tidak dipakai. Report tidak menyetujui pengambilalihan otomatis; semua calon mapping tetap `UNREVIEWED`. Rekomendasi: tinjau MATCH satu kali, lalu periksa perbedaan dan kandidat lain sebelum tahap implementasi berikutnya.

## Batasan yang disengaja

- Kedua server harus terbaca lengkap. Agent menggunakan opsi 5000 baris dari UI Devices dan membandingkan jumlah host unik dengan total yang ditampilkan. Jika lebih dari 5000 atau format halaman berubah, proses berhenti; tidak menyimpulkan device hilang.
- Daftar device meliputi seluruh server, bukan hanya tree DATIN. Detail ONT hanya dibaca untuk host yang namanya memuat SID sumber secara tepat, untuk menghindari ribuan request ping pada form edit.
- Pencarian SID menggunakan batas identifier, bukan substring angka. Device dengan SID salah/tidak tercantum pada description tidak dapat ditemukan melalui SN secara global pada tahap ini. NOT_FOUND perlu ditinjau, bukan otomatis CREATE.
- Perbandingan SN tidak membedakan huruf besar/kecil; slot/port/ONU dibandingkan sebagai bilangan bulat. IP OLT harus IP valid, slot/port/ONU positif dan SN wajib terisi.
- Keanggotaan header yang dibaca hanya pada tree DATIN 34/28. Host yang dipakai tree lain belum diperiksa, sehingga report tidak cukup untuk menyetujui penghapusan/perpindahan.
- Nama instansi menggunakan normalisasi dan alias eksplisit di master HD Work, bukan pencocokan samar.
- ATCS selalu dilindungi. Tambah header khusus lain dengan `--protected-header "NAMA HEADER"` (berlaku pada tree DATIN yang dibaca).
- Snapshot yang berubah selama scan ditandai `source_changed_during_scan = true` dan exit code 2. Laporan tetap tersimpan sebagai tinjauan bertanggal; ulangi sebelum menyetujui mapping. Sumber terlalu lama atau inventaris gagal dibaca menyebabkan exit code 1.
- Membuka form edit hanya membaca field konfigurasi; Cacti dapat melakukan ping otomatis saat halaman dibuka. Script tidak menekan Save dan tidak mengubah konfigurasi.
