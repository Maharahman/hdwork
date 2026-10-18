# Uji pembuatan dua device DATIN

Persetujuan pilot dibatasi pada SBS SID `1165651712` (header BANK JATIM 9127,
tree 34) dan SBU SID `1015518858` (header BANK JATIM 2929, tree 28).
Tidak membuat header, tidak mengubah host eksternal, tidak menjalankan batch,
edit massal, pemindahan, atau penghapusan. Pengujian belum dijalankan ke Cacti.

## Prasyarat server

Deploy route/service backend pilot, lalu jalankan migrasi sekali pada database
HD Work setelah migrasi mapping devices sebelumnya:

```bash
mysql -u root -p hd_work < database/migrations/20261019_add_datin_cacti_pilot_jobs.sql
pm2 restart hdwork-backend
```

Endpoint memakai key agent yang sama:

- GET `/api/datin-cacti/pilot-capabilities`: dukungan pilot dan target terbatas.
- POST `/api/datin-cacti/pilot-reservations`: reservasi unik SID sebelum create.
- POST `/api/datin-cacti/pilot-completions`: simpan mapping VERIFIED setelah read-back.

Jika endpoint/migrasi tidak tersedia, proses berhenti sebelum create. Reservasi
tidak kedaluwarsa dan tidak bisa diambil alih otomatis. Tabel job menahan SID
meskipun agent mati atau koneksi timeout. Mapping hasil disimpan dalam transaksi
bersama penyelesaian job. HD Work mempercayai bukti read-back dari agent VPN
terautentikasi; backend tidak mengakses Cacti secara mandiri.

## Di komputer agent

Laporan `pilot.report.json` harus sudah diperiksa. Kode mengecek ulang kedua SID,
sumber terkini, header, inventaris lengkap, serta field/default form sebelum
mengirim POST apa pun. Perubahan konfigurasi sumber dari laporan ditolak; buat
pratinjau baru dan minta persetujuan ulang bila target/config berubah.

```bash
# Default hanya preflight, tidak menulis reservasi ataupun Cacti.
/usr/local/bin/python3 integrations/cacti-agent/pilot_apply.py

# Hanya setelah backend siap, preflight lolos, dan dua target/config disetujui.
/usr/local/bin/python3 integrations/cacti-agent/pilot_apply.py --apply
```

Untuk setiap server: reservasi → simpan journal privat → create satu kali →
baca ulang host ID/konfigurasi → attach sekali → baca ulang tree dan host →
simpan mapping VERIFIED. CSRF berasal dari form terbaru; tidak dicetak ke log.
Form HTML Cacti lama memiliki field di luar elemen form, sehingga payload dibaca
dari dokumen lengkap yang sudah diperiksa sebagai halaman device baru.

## Jika ada kegagalan atau timeout

Berhenti tanpa retry create/attach otomatis. Server pertama mungkin sudah berhasil
meskipun server kedua belum. Jangan hapus reservasi/journal, menghapus device,
atau menjalankan ulang create untuk mencoba memperbaiki. Periksa phase dan
host ID di `pilot-SBS.report.json` / `pilot-SBU.report.json` (izin 0600,
diabaikan Git). Journal memuat token reservasi; jangan dibagikan atau diunggah.

Jika mapping completion timeout, penyelesaian job yang sama mendukung replay
idempoten dengan token dan receipt yang sama, tetapi create tidak boleh diulang.
Recovery harus membaca ulang host/tree sebelum mengirim completion. Tahap ini
belum menyediakan perintah recovery otomatis. Bila token hilang atau hasil POST
tidak pasti, tinjauan manual diperlukan; jangan membebaskan SID secara otomatis.

Setelah dua pilot VERIFIED, jalankan compare `--plan` kembali untuk meninjau
mapping. Sinkronisasi massal dan update terjadwal belum diaktifkan.
