# DATIN bulk create (manual, bukan sinkronisasi otomatis)

Mode ini membuat semua kandidat create yang valid, berurutan. Tidak melakukan
edit/hapus, adopsi host eksternal, pindah server/tree, atau mengelola ATCS.
Jumlah mengikuti sumber terbaru, bukan mengunci angka historis 273.
SID konflik, HOLD dan device yang sudah terpetakan tidak menjadi target create.

## Deploy

Deploy backend dan agent versi yang sama, lalu restart `hdwork-backend`.
Tidak ada SQL baru jika migrasi devices, pilot_jobs dan batch tanggal
20261019 telah terpasang. Endpoint `bulk-capabilities` harus tersedia;
404 atau backend lama menghentikan agent sebelum ada penulisan Cacti.
Key agent dan konfigurasi SBS/SBU memakai `.env` agent yang sudah ada.
Tidak perlu mengubah Apps Script atau collector untuk fitur ini.

## Pratinjau seluruh kandidat

```bash
python3 integrations/cacti-agent/bulk_sync.py --plan
```

Periksa `integrations/cacti-agent/bulk-plan.report.json`: daftar SID, server,
instansi, konfigurasi, header lama atau header baru, serta jumlah header unik.
File tidak berisi CSRF/password/token reservasi. File tetap privat karena
memuat inventaris pelanggan. Hash mengikat isi rencana; jangan edit manual.
Opsional `--limit 20` saat **plan saja** untuk membatasi kandidat, tanpa janji
pembagian seimbang per server. Tanpa `--limit` berarti semua kandidat.

## Eksekusi rencana tetap

```bash
python3 integrations/cacti-agent/bulk_sync.py --apply
```

Agent melakukan preflight kedua server, menyimpan token ke jurnal privat atomik
sebelum request reservasi, kemudian backend mereservasi seluruh SID dan header
baru dalam satu transaksi. Header yang sudah ada harus unik dan ID-nya cocok
dengan pratinjau; mapping yang berbeda tidak ditimpa. Header baru dipakai bersama
oleh semua SID instansi/server yang sama.

Setiap create/attach Cacti diberi jeda minimum 2 detik (`--delay 3` opsional),
dibaca ulang, kemudian mapping disimpan melalui receipt dari agent VPN.
Backend tidak mengakses Cacti langsung. Tidak ada pengiriman alarm uji;
collector dan penjadwal Telegram berjalan seperti biasa, sehingga device baru
yang benar-benar down bisa menghasilkan alarm normal.

## Jika terputus

```bash
python3 integrations/cacti-agent/bulk_sync.py --resume
```

Gunakan mesin, jurnal, dan backend yang sama. Jangan jalankan salinan jurnal
di komputer lain bersamaan. Lock lokal mencegah proses agent saling tumpang
tindih di mesin yang sama; reservasi unik database mencegah pekerjaan lain
membuat ulang SID yang sama. Reservasi tidak kedaluwarsa otomatis.

Replay reservasi/mapping idempoten. Device yang POST-nya timeout dicari memakai
prefix/SID lalu konfigurasi dan keanggotaan tree diverifikasi sebelum lanjut.
POST Cacti **tidak pernah diulang otomatis**. Jika host tidak ditemukan setelah
create pernah dicoba, hasil attach masih hilang, header baru belum sempat dicatat
ID-nya, ada duplikat, atau sumber berubah, pekerjaan berhenti untuk pemeriksaan
manual. `--resume` bukan pemaksaan melewati keadaan yang belum pasti.
Ini sengaja berbeda dari mengulang create dan berisiko duplikasi.

Jurnal `bulk-execution.report.json` memuat token, mode 0600, ditulis melalui
temp-file + fsync + rename. Jangan unggah, bagikan, hapus, atau ubah jurnal untuk
memaksa pengulangan. File `.report.json` sudah diabaikan Git oleh konfigurasi
folder agent. Jurnal yang selesai tetap disimpan sebagai bukti; pekerjaan baru
harus memakai pasangan `--report` dan `--journal` baru, tanpa menimpa yang lama.

Sesudah selesai jalankan `compare_devices.py --plan`. Target baru seharusnya
MATCH; perubahan spreadsheet yang terjadi sesudah pembuatan perlu ditinjau
terpisah. Fitur edit/hapus dan penjadwalan otomatis belum termasuk.
