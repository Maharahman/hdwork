# Agent otomatis header instansi DATIN

Script `integrations/cacti-agent/sync_headers.py` mengambil daftar instansi dari master spreadsheet HD Work, login ke SBS/SBU, membaca header yang ada, dan pada mode `--apply` membuat header yang belum tersedia. Agent tidak menambah/mengubah/menghapus host atau menghapus/mengganti nama header lama.

## Backend

1. Deploy kode dan jalankan migrasi `database/migrations/20261018_add_datin_cacti_headers.sql` satu kali. Backend baru memerlukan migrasi ini sebelum menerima sinkron spreadsheet berikutnya.
2. Buat key khusus dengan `openssl rand -hex 32` dan tambahkan pada `.env` backend:

```env
DATIN_CACTI_AGENT_API_KEY=isi_key_agent_yang_baru
# Opsional, batas umur snapshot default 30 menit:
DATIN_CACTI_SOURCE_MAX_AGE_SECONDS=1800
```

3. Restart backend. Jalankan sinkron spreadsheet sekali atau tunggu trigger 10 menit. Kolom `valid_snapshot_at` baru terisi saat sinkron berikutnya. Sebelum itu pratinjau tidak menampilkan instansi.

Endpoint khusus agent memakai header `X-Cacti-Agent-Key`, terpisah dari key Apps Script/collector:
- `GET /api/datin-cacti/header-plan`: instansi per server, jumlah pelanggan dan snapshot sumber.
- `POST /api/datin-cacti/header-mappings`: simpan ID header yang sudah diverifikasi agent.

## Komputer agent

Gunakan satu komputer Linux/macOS yang terhubung VPN dan dapat menjangkau kedua alamat Cacti. Boleh komputer collector yang sama. Tidak perlu Google Drive/service account/Chrome/Selenium.

```bash
cd /path/ke/hdwork/integrations/cacti-agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Isi `.env` dengan key agent yang sama seperti backend dan kredensial Cacti SBS/SBU. File ini diabaikan Git. Jangan menyalin kredensial ke kode.

Jalankan pratinjau:

```bash
.venv/bin/python sync_headers.py --report preview.report.json
```

Hasil `existing` berarti header sudah ada; `would_create` berarti belum ada dan belum dibuat. Periksa daftar namanya sebelum mode apply pertama.

Untuk menerapkan, dapat dimulai satu server:

```bash
.venv/bin/python sync_headers.py --apply --server SBS --report sbs.report.json
.venv/bin/python sync_headers.py --apply --server SBU --report sbu.report.json
```

Atau sekaligus kedua server:

```bash
.venv/bin/python sync_headers.py --apply --report last.report.json
```

Agent membaca ulang halaman sesudah Create dan mengambil ID header sebenarnya. Retry POST Create tidak dilakukan otomatis saat timeout. Jalankan ulang agent; ia mencari header yang sudah terbentuk sebelum membuat baru. Mapping yang gagal dikirim ke HD Work dapat disimpan lagi lewat run berikutnya.

## Jadwal opsional

Setelah pratinjau dan apply pertama terverifikasi, pasang cron hanya pada satu komputer agent. Contoh jalur deployment CT (sesuaikan jika collector di komputer lain):

```cron
5,15,25,35,45,55 * * * * /root/HD/hdwork/integrations/cacti-agent/.venv/bin/python /root/HD/hdwork/integrations/cacti-agent/sync_headers.py --apply --report /root/HD/hdwork/integrations/cacti-agent/last.report.json >> /root/HD/hdwork/integrations/cacti-agent/agent.log 2>&1
```

Agent berjalan sekali lalu keluar. Lock lokal mencegah dua proses di komputer sama. Jangan menjadwalkan apply dari beberapa komputer secara bersamaan karena Cacti tidak menjamin nama header unik. Apps Script dan cron memiliki jadwal independen; agent memakai snapshot terakhir yang sudah sukses.

## Aturan

- Tree SBS tetap 34; SBU tetap 28. Nama tree diperiksa sebelum tindakan.
- Header dicocokkan dengan trim, huruf kapital, dan spasi konsisten. Tidak menebak alias instansi berbeda. Samakan sumber/alias penamaan sebelum apply jika ejaannya memang berbeda.
- Hanya instansi dengan baris OK, SID unik, SERVER SBS/SBU, dan lolos snapshot terakhir yang diproses. Baris NOT/konflik yang masih tersimpan di master lama tidak diekspor ke agent.
- Snapshot lebih tua dari 30 menit ditolak. Data kosong tidak menghapus apa pun.
- Header sudah ada digunakan kembali. Header bernama sama lebih dari satu ditahan untuk pemeriksaan manual.
- Saat VPN/login/error verifikasi gagal, agent berhenti dengan exit code 1. Tidak mengirim Telegram.
- Aksi Cacti dan penyimpanan mapping bukan satu transaksi lintas sistem. Jika mapping gagal setelah header dibuat, run berikutnya akan menemukan header tersebut dan menyimpan ulang ID-nya.

```sql
SELECT server, tree_id, institution_name, header_id, last_verified_at
FROM datin_cacti_headers ORDER BY server, institution_name;
```
