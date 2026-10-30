# Paid Promote Checker

Program command-line untuk memeriksa apakah daftar akun Instagram sudah mengerjakan penugasan *paid promote* hari ini (Story dan/atau Feed). Program membuka tiap akun di browser, mencari hashtag, link sticker, caption, dan tag akun yang diwajibkan, lalu menandai yang kurang. Operator mengonfirmasi tiap temuan, dan hasilnya dikirim ke Google Sheets (denda) dan Google Docs (laporan grup).

> **Gunakan dengan bertanggung jawab.** Periksa hanya akun yang pemiliknya sudah setuju. Mengotomatisasi Instagram bisa melanggar Ketentuan Layanan Instagram, risiko ditanggung pribadi. Sebaiknya pakai akun khusus untuk sesi login. Lihat [Privasi dan keamanan](#privasi-dan-keamanan).

## Daftar isi

1. [Kebutuhan](#kebutuhan)
2. [Instalasi](#instalasi)
3. [Konfigurasi sekali di awal](#konfigurasi-sekali-di-awal)
4. [Cara memakai setiap hari](#cara-memakai-setiap-hari)
5. [Mengisi tugas harian](#mengisi-tugas-harian)
6. [Review hasil](#review-hasil)
7. [Hasil keluaran](#hasil-keluaran)
8. [Pengaturan lanjutan](#pengaturan-lanjutan)
9. [Mengatasi masalah](#mengatasi-masalah)
10. [Privasi dan keamanan](#privasi-dan-keamanan)

## Screenshots
<img width="1775" height="603" alt="Main Page" src="https://github.com/user-attachments/assets/5a6daa3f-2f48-4b8e-a6ca-c1a0b7b5ffc7" />
<img width="763" height="542" alt="Page 1" src="https://github.com/user-attachments/assets/7e79edd3-13fd-4fd1-91ab-704cb5716971" />
<img width="881" height="747" alt="Page 2" src="https://github.com/user-attachments/assets/dc3cfb2f-0e24-4326-a06f-19e31cb8f5a6" />


## Kebutuhan

- Python 3.12
- Google Chrome terpasang (Playwright memakai Chrome yang ada, jadi `playwright install` tidak perlu)
- Akun layanan (*service account*) Google Cloud dengan Google Sheets API, Docs API, dan Drive API aktif
- Opsional: GPU NVIDIA untuk OCR lebih cepat (tanpa GPU, OCR berjalan di CPU)
- Windows adalah platform utama. Di macOS/Linux, sinyal mulai hanya lewat ENTER.

## Instalasi

```powershell
git clone https://github.com/JienFran/ivy_paid_promote_checker.git
cd ivy_paid_promote_checker

python -m venv venv
.\venv\Scripts\activate

# Opsional, untuk OCR dengan GPU. Pasang torch lebih dulu
# (torch dari PyPI di Windows hanya versi CPU):
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128

pip install -r requirements.txt
```

## Konfigurasi sekali di awal

Ada empat hal yang harus disiapkan. Semuanya berada di folder utama project (sejajar dengan `main.py`).

### 1. `cookies.json` — sesi login Instagram

Program tidak login dengan password. Ia memakai cookies dari sesi yang sudah login. Ekspor cookies dari browser yang sedang login ke akun Instagram, dalam bentuk array JSON dengan format yang diterima `add_cookies` milik Playwright, lalu simpan sebagai `cookies.json`.

Jika sesi kedaluwarsa atau Instagram meminta verifikasi (*checkpoint*), program berhenti dengan pesan error sesi. Ekspor cookies yang baru lalu jalankan lagi.

### 2. `credentials.json` — akun layanan Google

1. Di Google Cloud, buat *service account*, buat key berformat JSON, dan simpan sebagai `credentials.json`.
2. Aktifkan Google Sheets API, Docs API, dan Drive API pada project yang sama.
3. Bagikan spreadsheet dan Google Doc tujuan ke alamat `client_email` yang ada di `credentials.json`, dengan peran **Editor**.
4. Buat tab bernama `ARENA` dan `VAULT` di **spreadsheet maupun dokumen**. API Docs tidak bisa membuat tab, jadi tab harus sudah ada.

Kolom yang ditulis ke Sheets untuk tiap denda: `A` No, `B` Nama, `C` Tanggal, `D` Pelanggaran, `E` Denda tambahan (diisi "Tdk ada"), `F` Bukti (kosong), `G` Nominal denda, `H` Pembayaran (diisi "BELUM BAYAR"), `I` Jenis penugasan.

### 3. `.env` — pengaturan pribadi

Salin `.env.example` menjadi `.env`, lalu isi.

| Variabel | Fungsi |
|---|---|
| `SPREADSHEET_NAME` | Judul (atau URL) spreadsheet tujuan denda. **Wajib** |
| `DOC_NAME` | Judul (atau URL) Google Doc tujuan laporan grup |
| `GATEKEEPER_KEYWORDS` | Hashtag yang harus ada di story/post agar dihitung sebagai tugas. Pisahkan dengan koma, tanpa `#`. **Wajib** |
| `STAGING_SHEET_URL`, `OFFICIAL_SHEET_URL` | Link spreadsheet yang dicetak di akhir proses (opsional) |
| `REPORT_TITLE`, `REPORT_GREETING`, `REPORT_INTRO` | Judul, sapaan, dan pembuka laporan grup |
| `REPORT_PAYMENT_TERMS` | Ketentuan pembayaran di akhir laporan, satu aturan per baris. Dilewati jika kosong |

Contoh:

```
STAGING_SHEET_URL=https://docs.google.com/spreadsheets/d/...
OFFICIAL_SHEET_URL=https://docs.google.com/spreadsheets/d/...
SPREADSHEET_NAME=...
DOC_NAME=...
GATEKEEPER_KEYWORDS=indonesia

REPORT_TITLE="Report Denda"
REPORT_GREETING="Selamat pagi"
REPORT_INTRO="Berikut adalah daftar nama yang belum memenuhi ketentuan untuk konten Paid Promote & Buzzer."
REPORT_PAYMENT_TERMS="Transfer ke..."
```

### 4. `targets.json` — daftar akun yang diperiksa

Salin `targets.example.json` menjadi `targets.json`, lalu isi akun per divisi:

```json
[
  {
    "divisi": "VAULT",
    "anggota": [
      {
        "username": "username",
        "nama": "Nama Lengkap"
      }
    ]
  },
  {
    "divisi": "ARENA",
    "anggota": [
      {
        "username": "zuck",
        "nama": "Mark Zuckerberg"
      }
    ]
  }
]
```

- Tanda `@` di depan username akan dibuang otomatis.
- Divisi yang namanya mengandung kata `VAULT` dikirim ke tab `VAULT`. Divisi lain dikirim ke tab `ARENA`.

## Cara memakai setiap hari

Jalankan selalu dari folder utama project:

```powershell
.\venv\Scripts\activate
python main.py
```

1. **Isi tugas harian.** Jika setup hari ini belum ada, program menanyakannya ([lihat bagian berikutnya](#mengisi-tugas-harian)). Jawabannya disimpan di `daily_state.json` dan dipakai lagi sampai hari berganti.
2. **Tunggu sinyal mulai.** Tekan SPACE atau ENTER untuk mulai. Jika dibiarkan, program mulai sendiri satu detik setelah jam penugasan berakhir (default 11:00–22:15 WIB).
3. **Pemeriksaan akun.** Browser Chrome terbuka dan memeriksa tiap akun. Story dan Feed bisa diperiksa bersamaan di dua tab.
4. **Review.** Tiap temuan ditampilkan untuk dikonfirmasi ([lihat Review hasil](#review-hasil)).
5. **Ekspor.** Denda yang dikonfirmasi masuk ke Google Sheets, dan laporan grup masuk ke `report/` dan Google Docs.

### Pre-check dan final

- **Sebelum jam penugasan berakhir**, program berjalan sebagai *pre-check*. Akun yang sudah **Benar** disimpan di `checked_accounts.json` dan dilewati pada run berikutnya. Akun lain dibiarkan, dan **tidak ada denda** yang dibuat.
- **Setelah jam penugasan berakhir**, jalankan lagi untuk *final*. Program hanya memeriksa akun yang belum tersimpan sebagai Benar, lalu masuk ke tahap review dan ekspor.

`daily_state.json` dan `checked_accounts.json` otomatis dianggap kosong saat tanggal berganti. Jika ingin mengisi ulang setup di hari yang sama, hapus `daily_state.json` lalu jalankan lagi.

## Mengisi tugas harian

Program menanyakan hal berikut untuk setiap tugas:

| Pertanyaan | Penjelasan |
|---|---|
| Total tasks | Jumlah tugas hari ini (ENTER = 1) |
| Type | `1` = Story, `2` = Feed |
| Required hashtags | Satu hashtag per baris. Baris kosong untuk selesai, `0` jika tidak ada |
| Sticker link label | Khusus Story. Label link sticker yang diwajibkan, `0` jika tidak ada |
| Required text/keyword (Story) atau caption (Feed) | Tempel teks, boleh beberapa baris, lalu ketik `END` di baris sendiri. Ketik `0` lalu `END` jika tidak ada |
| Account tag (Feed) | `1` = wajib men-tag akun, `2` = tidak boleh, `0` = tidak diperiksa |
| Call to action (Story) | `1` = wajib ada kalimat ajakan bebas (hanya dicek ada atau tidaknya, isinya tidak dinilai), `0` = tidak diperiksa |

Catatan:
- Di hashtag, tulis `_DIVISI` untuk diganti otomatis dengan nama divisi akun (huruf besar, tanpa spasi). Contoh: `NamaEvent_DIVISI` untuk anggota divisi "ARENA Alpha" diperiksa sebagai `NamaEvent_ARENAALPHA`.
- Beberapa tugas dengan kriteria yang sama harus dipenuhi oleh slide atau post yang **berbeda**. Satu slide tidak dihitung untuk dua tugas.
- Story dan post hanya dihitung jika memuat **semua** hashtag di `GATEKEEPER_KEYWORDS`.
- Pada Feed, Instagram hanya menjadikan 5 hashtag pertama di caption sebagai link. Hashtag setelahnya dicocokkan sebagai teks biasa.

## Review hasil

Setiap temuan ditampilkan dengan link akun, divisi, status, dan denda. Pilihan yang tersedia:

| Tombol | Aksi |
|---|---|
| ENTER | Konfirmasi status yang terdeteksi |
| `p` | Pass (tidak ada denda) |
| `s` | Tandai sebagai Kesalahan |
| `t` | Tandai sebagai Tidak mengerjakan |

Untuk status Kesalahan, program meminta keterangan singkat. Keterangan ini hanya masuk ke laporan grup, tidak ke Google Sheets. Item *Manual review* tidak punya status terdeteksi, jadi ENTER tidak diterima untuk item tersebut.

| Status | Arti | Denda (default) |
|---|---|---|
| Benar | Tugas dikerjakan dengan benar | tidak ada |
| Kesalahan | Dikerjakan, tapi salah | Rp10.000 per tugas |
| Tdk Post | Story/post yang sesuai tidak ditemukan | Rp25.000 per tugas |
| Manual Review | Program tidak bisa memutuskan, misalnya akun privat atau username salah | Rp0 sampai dikonfirmasi |

## Hasil keluaran

- **Google Sheets:** satu baris per denda di tab `ARENA` atau `VAULT`.
- **Google Docs:** laporan grup per divisi ditambahkan di bagian bawah tab `ARENA` atau `VAULT`.
- **`report/report_arena.txt` dan `report/report_vault.txt`:** salinan laporan yang sama. File lama dihapus jika divisi itu tidak punya pelanggaran.

## Pengaturan lanjutan

Beberapa pengaturan ada di bagian atas `main.py`:

| Pengaturan | Fungsi |
|---|---|
| `STORY_START_HOUR`, `STORY_START_MINUTE`, `STORY_END_HOUR`, `STORY_END_MINUTE` | Jam penugasan (WIB) |
| `STORY_TARGET_DATE` | Isi `"YYYY-MM-DD"` untuk memeriksa story keesokan harinya (story aktif 24 jam) |
| `NOMINAL` | Besar denda per status |
| `USE_PARALLEL_TABS` | Periksa Story dan Feed bersamaan di dua tab |
| `USE_OCR_FALLBACK` | OCR sebagai cadangan terakhir untuk teks yang tergambar di gambar |
| `MAX_FEED_POSTS_SCAN`, `FEED_POSTS_PER_TASK_SCAN` | Batas jumlah post Feed terbaru yang dibuka |

Uji koneksi satu per satu:

```powershell
python core/browser.py    # membuka Instagram dengan cookies Anda
python core/sheets.py     # menghubungkan ke tab ARENA dan VAULT di spreadsheet
python core/docs.py       # menghubungkan ke tab ARENA dan VAULT di dokumen, menulis satu baris [TEST] ke ARENA
```

## Mengatasi masalah

| Pesan atau gejala | Penyebab dan solusi |
|---|---|
| `GATEKEEPER_KEYWORDS is not set in .env` | Isi `GATEKEEPER_KEYWORDS` di `.env`. Tanpa ini semua story/post akan dianggap tugas, jadi program berhenti |
| `SPREADSHEET_NAME is not set in .env` | Isi `SPREADSHEET_NAME` di `.env` |
| `Document not set: ... DOC_NAME` | Isi `DOC_NAME` di `.env` |
| `targets.json not found` | Salin `targets.example.json` menjadi `targets.json` lalu isi |
| `credentials.json not found` | Letakkan file key akun layanan di folder utama |
| `Google Doc '...' not found` atau spreadsheet tidak ditemukan | Bagikan dokumen/spreadsheet ke `client_email` di `credentials.json` dengan peran Editor |
| `Tab '...' not found` | Buat tab `ARENA` dan `VAULT` secara manual di dokumen dan spreadsheet |
| `cookies.json not found` atau error sesi Instagram | Ekspor cookies baru dari browser yang sedang login |
| Akun masuk *Manual Review* dengan catatan "private account" atau "page unavailable" | Akun privat, username salah, atau akun dinonaktifkan. Periksa manual |
| OCR lambat atau ada peringatan CUDA | Torch yang terpasang versi CPU. Pasang torch CUDA seperti di bagian Instalasi, atau biarkan berjalan di CPU |
| Setup hari ini salah | Hapus `daily_state.json` lalu jalankan ulang |

## Privasi dan keamanan

- `.env`, `cookies.json`, `credentials.json`, `targets.json`, `daily_state.json`, `checked_accounts.json`, dan folder `report/` berisi rahasia atau data pribadi. Semuanya sudah ada di `.gitignore`. **Jangan pernah di-commit.**
- Cookies sesi memberi akses penuh ke akun Instagram. Jika pernah terekspos, logout dari semua perangkat dan ganti password.
- Jika key akun layanan terekspos, hapus key itu di Google Cloud dan buat yang baru.
- Dapatkan persetujuan dari pemilik akun yang diperiksa, dan simpan daftar akun serta laporan secara privat.
