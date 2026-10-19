# Uji hapus manual satu host DATIN

`integrations/cacti-agent/delete_device.py` hanya untuk SID `1165651712`,
host integrasi SBS `8471`, DATIN tree `34`, header BANK JATIM `9127`.
Tidak menghapus OLT/ONT fisik, header, host lain, atau ATCS. Ini bukan
sinkronisasi hapus otomatis dan tidak melakukan pembersihan database HD Work.

## Bukti NOT: batasan payload saat ini

Apps Script mengirim data pelanggan hanya untuk baris OK. Inventaris instansi
memuat customer_name/status/source_row tetapi **belum mengirim SID baris NOT**.
Karena itu ketiadaan SID dari endpoint device-source bukan bukti NOT dan tidak
boleh menjadi pemicu penghapusan otomatis (bisa akibat konflik, data kosong,
pemindahan baris, atau sumber parsial).

Pilot ini memakai otorisasi manusia yang eksplisit: pengguna menyatakan SID
tersebut diubah ke NOT, sudah disinkronkan, dan memberi screenshot barisnya.
Flag `--confirm-not` adalah pencatatan bukti manual itu, bukan mekanisme
verifikasi status NOT di server. Jangan dipakai sebagai penghapus massal.

Sebelum penghapusan otomatis dapat dibuat, Apps Script/backend harus menyimpan
inventaris status lengkap dengan SID dari snapshot tervalidasi, membedakan NOT
dari hilang/konflik, serta lifecycle reservasi/tombstone server-side. Hal tersebut
belum termasuk pilot ini. Tidak ada migrasi atau perubahan Apps Script di sini.

## Pengaman pilot

- Snapshot harus masih segar menurut backend; SID tidak boleh OK atau konflik.
- Mapping harus VERIFIED dan identik dengan SID/server/host/tree/header tetap.
- Nama host wajib `[HD-D] 1165651712_BANK JATIM_KF KENJERAN`.
- Periksa semua tree yang terlihat pada akun agent; daftar tree harus sama
  dengan opsi tree pada menu Devices. Tolak host yang dipakai tree lain.
  Ini cakupan akun tersebut, bukan klaim akses terhadap tree tersembunyi.
- Simpan konfigurasi publik sebelum hapus (IP, slot/port/ONU, SN, description)
  dan hash opsi non-target, tanpa password/CSRF/SNMP community.
- Form pemilihan wajib hanya memuat checkbox host `8471`. Form konfirmasi wajib
  tepat satu serialized ID `8471`, nama tepat, dan action Delete yang dikenal.
- Pilih **Leave all Graph(s) and Data Source(s) untouched**, bukan penghapusan
  graph/data source. Label Cacti menjelaskan data source dapat dinonaktifkan
  oleh opsi ini. Akun agent tidak memiliki akses halaman Graph Management;
  jangan menyimpulkan jumlah graph nol berdasarkan kegagalan akses tersebut.
- Baca ulang konfigurasi dan sumber sebelum konfirmasi final. Jangan edit
  host/sumber atau menjalankan agent lain bersamaan; tidak ada global CAS.
- Simpan intent atomik privat 0600 sebelum setiap POST berisiko. Konfirmasi
  final tidak diulang otomatis setelah timeout.
- Verifikasi melalui inventaris Devices lengkap bahwa hanya ID target yang
  hilang, ID host lain tetap, seluruh header tetap, dan tidak ada link host
  target pada DATIN SBS. Perubahan paralel menyebabkan audit ditahan.

## Perintah untuk uji yang telah disetujui

```bash
python3 integrations/cacti-agent/delete_device.py --plan --confirm-not
python3 integrations/cacti-agent/delete_device.py --apply --confirm-not
```

Default journal `delete-1165651712-execution.report.json` dan pratinjau
`delete-1165651712-plan.report.json` disimpan privat dan diabaikan Git.
`--prepare` hanya mengambil halaman konfirmasi untuk inspeksi tanpa mengirim
konfirmasi final. `--apply` dapat melanjutkan jurnal CONFIRMATION_PREPARED
yang identik, tetapi tidak mengulang konfirmasi yang pernah dicoba.

```bash
python3 integrations/cacti-agent/delete_device.py --resume --confirm-not
```

Resume hanya memverifikasi ketiadaan host dan audit. Tidak mengirim POST delete.
Jika host masih ditemukan atau inventaris berubah, hentikan untuk pemeriksaan.
Jangan hapus jurnal untuk memaksa pengulangan.

## Riwayat HD Work dan pemulihan

Mapping host, pilot_jobs, customer master historis, dan alarm historis HD Work
**tetap disimpan**. Customer tersebut tidak ada pada sumber OK terbaru karena
valid_snapshot_at historis tidak sama dengan snapshot terbaru. Tidak mengklaim
master database sudah dihapus atau mapping otomatis ditandai DELETED.
Retensi mapping juga membuat perubahan NOT kembali OK ditahan untuk rekonsiliasi,
bukan membuat ulang otomatis memakai host yang hilang.

Delete tidak memiliki undo otomatis. Backup konfigurasi membantu rekonstruksi
atas permintaan baru, tetapi host baru mungkin mendapat ID berbeda dan mapping
perlu direkonsiliasi secara eksplisit. Mengubah status kembali OK saja tidak
mengembalikan host yang telah dihapus. Riwayat RRD tidak dibackup oleh script ini.
