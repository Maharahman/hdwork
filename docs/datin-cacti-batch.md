# Pratinjau batch 10 device dan header baru

```bash
/usr/local/bin/python3 integrations/cacti-agent/batch_preview.py
```

Ini hanya membaca HD Work dan Cacti. Output lokal `batch-10.report.json` berisi
tepat 10 calon device (5 SBS, 5 SBU), konfigurasi form live, serta daftar dependensi
header unik. Tidak ada POST create/reservasi/mapping. Login Cacti tetap menggunakan
alur login biasa. Script tidak mempunyai `--apply` atau opsi memperbesar jumlah.

## Kriteria pemilihan

- STATUS sumber OK, data valid termasuk nilai lokasi 0.
- Berasal dari snapshot valid terbaru, bukan SID konflik.
- Rencana harus tepat CREATE_HEADER + CREATE_DEVICE + ATTACH_TO_TREE.
- Tidak ada mapping/host ID DATIN; kandidat eksternal tidak menjadi target.
- Header instansi belum ada pada tree tujuan. ATCS/monitoring khusus ditahan.
- Data harus muat dalam batas form create yang dibaca dari Cacti, tanpa pemotongan.
- Mengutamakan beberapa SID per instansi untuk menguji header bersama, tetapi
  identitas header tetap dipisahkan berdasarkan server dan nama instansi.
- Jika 5 calon valid per server tidak tersedia, berhenti. Tidak mengganti dengan
  device yang sudah ada/header lama, aksi UPDATE, atau mengambil alih eksternal.

Ringkasan `CREATE_HEADER_UNIQUE` menghitung header unik, bukan jumlah SID.
Laporan tidak menyimpan kredensial/CSRF. Header, sumber dan mapping dibaca kembali
sebelum laporan diterbitkan; perubahan selama scan menyebabkan proses ditolak.
Inventaris kandidat bertanda yang sudah ada tidak diambil alih otomatis.

## Batas tahap ini

Daftar 10 SID/header harus dikonfirmasi sebelum tahap tulis. Endpoint dan executor
pilot yang sudah dipasang masih dibatasi dua SID Bank Jatim terdahulu. Jangan
mengganti file pilot atau mengedit targetnya untuk mencoba melewati batas tersebut.

## Executor batch terbatas

Daftar di bawah telah disetujui sebagai satu batch saja. Manifest disimpan sebagai
SID/server/instansi dan hash konfigurasi di `src/config/datin-cacti-batch.json`.
IP/SN lengkap tidak dimasukkan ke konfigurasi Git; hash mengikat nilainya beserta
Description dan opsi form yang telah dipratinjau. Perubahan konfigurasi sumber,
target, atau laporan membuat apply ditolak, bukan dianggap sebagai persetujuan baru.

| Server | Header baru | SID |
| --- | --- | --- |
| SBS | BANK DANAMON | 2091577385, 2094788923, 2094789069 |
| SBS | BANK CENTRAL ASIA | 2098959062, 1110515952 |
| SBU | BANK CENTRAL ASIA | 1711507079, 1713792553, 1736952668 |
| SBU | DISKOMINFO KAB SUMENEP | 3001068269-0031072592, 3001068269-0031073611 |

### Deploy dahulu

Backend baru memerlukan migrasi berikut sekali saja, setelah tabel pilot dan
mapping sebelumnya. Jangan menjalankan ulang migrasi lama.

```bash
mysql -u root -p hd_work < database/migrations/20261019_add_datin_cacti_batch.sql
pm2 restart hdwork-backend
```

Route memakai key agent yang sama: GET `/api/datin-cacti/batch-capabilities`, POST
`batch-reservations`, `batch-headers`, dan `batch-completions`. Penyelesaian setiap
device tetap memakai `pilot-completions`, dengan header yang sudah diikat.
Endpoint/migrasi yang belum tersedia menghentikan proses sebelum create.

### Preflight dan eksekusi

Gunakan laporan `batch-10.report.json` yang disetujui, bukan mengganti target
otomatis dari hasil pratinjau yang lebih baru. Agent memerlukan seluruh checkout
repo agar konfigurasi hash sama dengan backend.

```bash
# Default hanya membaca; tidak membuat reservasi.
/usr/local/bin/python3 integrations/cacti-agent/batch_apply.py

# Setelah backend siap, persetujuan 10 target sudah ada dan preflight lolos.
/usr/local/bin/python3 integrations/cacti-agent/batch_apply.py --apply
```

Alur: cek sumber/header/inventaris/form → reservasi atomik satu batch, 4 header,
dan 10 SID → buat setiap header sekali → baca ID dan ikat ke semua job pada header
tersebut → create/attach/read-back device satu per satu → simpan mapping VERIFIED
→ verifikasi kelengkapan batch. Header BANK CENTRAL ASIA di SBS dan SBU berbeda
reservasinya. Header yang muncul di tengah proses atau mempunyai mapping lama
ditahan untuk rekonsiliasi, bukan diadopsi/ditimpa otomatis.

Reservasi tidak kedaluwarsa otomatis. Satu batch ID tidak bisa dijalankan ulang
untuk membuat batch kedua. Source data diperiksa kembali sebelum menulis setiap
header/device. Timeout atau hasil tidak pasti menyebabkan proses berhenti, tanpa
retry POST create/attach. Sebagian header/device mungkin sudah berhasil; jangan
menghapus hasil atau journal untuk mencoba ulang. Kegagalan transaksi reservasi
menahan seluruh batch sebelum penulisan apa pun ke Cacti.

Journal privat 0600: `batch10-execution.report.json` serta
`batch10-SERVER-SID.report.json`. Memuat token reservasi; jangan unggah atau
bagikan. Journal diabaikan Git. Recovery harus membaca ulang hasil live dan
merekonsiliasi reservasi, bukan mengulang create; belum ada perintah recovery
otomatis. Setelah sukses jalankan compare `--plan` untuk memastikan 12 mapping
(dua pilot terdahulu dan sepuluh batch) tidak lagi direncanakan CREATE.

Ini belum mengaktifkan sinkronisasi massal 283 calon lain, update otomatis,
pemindahan, penghapusan, atau scheduler. ATCS/device eksternal tetap tidak disentuh.
