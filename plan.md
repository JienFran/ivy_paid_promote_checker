# Plan: Gambaran Project

Dokumen ini merangkum apa saja yang bisa dilakukan project **Paid Promote Checker**, dari awal sampai akhir. Untuk cara memakainya, lihat [README.md](README.md).

## Tujuan

Mengganti pemeriksaan manual penugasan *paid promote* (membuka akun satu per satu, mencari hashtag, mencatat denda) dengan alur semi-otomatis: program memeriksa dan menandai, operator hanya mengonfirmasi, dan hasilnya langsung tercatat di Google Sheets dan Google Docs.

## Alur besar

```
Setup harian ─► Menunggu sinyal mulai ─► Pemeriksaan akun ─► Review operator ─► Ekspor Sheets + laporan Docs
```

## Kemampuan

### 1. Setup tugas harian
- Mendukung banyak tugas dalam satu hari, masing-masing bertipe **Story** atau **Feed**
- Kriteria per tugas: hashtag wajib, label link sticker, teks/kata kunci (Story) atau caption (Feed), tag akun pada foto/video (Feed), dan kalimat ajakan bebas (Story)
- Input teks beberapa baris (caption panjang) dengan penutup `END`
- Placeholder `_DIVISI` pada hashtag, diganti otomatis dengan nama divisi tiap akun
- Setup tersimpan di `daily_state.json` dan otomatis dianggap kedaluwarsa saat tanggal berganti

### 2. Daftar akun dan pembagian divisi
- Daftar akun dibaca dari `targets.json`, dikelompokkan per divisi
- Username dinormalisasi (tanda `@` dibuang)
- Divisi `VAULT` dan divisi lain (`ARENA`) dipisah ke tab dan laporan yang berbeda

### 3. Pemeriksaan Story
- Membuka story tiap akun dan melewati semua slide, bukan hanya slide pertama dan terakhir
- Mengabaikan highlight pada tray story
- Hanya menghitung slide yang terbit di dalam jendela waktu penugasan (default 11:00–22:15 WIB), termasuk membaca waktu relatif Instagram seperti "5h", "2 jam", "30m"
- Slide dianggap item tugas hanya jika memuat **semua** hashtag *gatekeeper*
- Hashtag, mention, dan link dibaca dari beberapa sumber: elemen halaman, respons JSON Instagram sendiri, teks, dan OCR sebagai cadangan terakhir untuk teks yang tergambar langsung di gambar
- Cek link sticker lewat OCR, dengan pelewatan otomatis untuk slide yang jumlah hitbox sticker-nya terlalu sedikit (menghemat waktu)
- Cek keberadaan kalimat ajakan bebas lewat OCR (hanya ada atau tidak, isinya tidak dinilai)
- Pencocokan teks dan hashtag toleran terhadap salah baca OCR kecil (*fuzzy match*)
- Menangkap kartu story dalam resolusi tinggi agar OCR lebih akurat

### 4. Pemeriksaan Feed
- Membuka beberapa post terbaru tiap akun (jumlahnya dibatasi) dan berhenti setelah beberapa post berturut-turut yang sudah lewat dari jendela waktu
- Mengecek hashtag, caption (teks panjang), dan tag akun pada foto/video (wajib, terlarang, atau tidak diperiksa)
- Menangani batas Instagram yang hanya menjadikan 5 hashtag pertama di caption sebagai link, dengan mencocokkan sisanya sebagai teks biasa

### 5. Logika penilaian
- Empat status: **Benar**, **Kesalahan**, **Tdk Post** (tidak mengerjakan), dan **Manual Review**
- Beberapa tugas dengan kriteria yang sama harus dipenuhi slide/post yang berbeda
- Akun privat, username salah, atau akun nonaktif otomatis masuk *Manual Review*
- Denda dihitung per tugas dan per status (nominal bisa diubah di `main.py`)

### 6. Kecepatan dan efisiensi
- Story dan Feed bisa diperiksa bersamaan di dua tab browser
- **Pre-check** sebelum jendela waktu berakhir: akun yang sudah Benar disimpan di `checked_accounts.json` dan tidak diperiksa ulang pada run berikutnya, tanpa denda apa pun
- **Final** setelah jendela waktu berakhir: hanya memeriksa akun yang belum Benar
- Mulai otomatis tepat setelah jendela waktu berakhir, atau lebih awal dengan SPACE/ENTER

### 7. Review operator
- Tiap temuan ditampilkan dengan link akun, divisi, status, dan denda
- Tombol seragam: ENTER (konfirmasi), `p` (pass), `s` (Kesalahan), `t` (Tidak mengerjakan)
- Untuk Kesalahan, operator mengisi keterangan yang hanya masuk ke laporan grup

### 8. Keluaran
- **Google Sheets:** satu baris per denda di tab `ARENA` atau `VAULT`, lengkap dengan nomor urut, tanggal, jenis pelanggaran, nominal, dan status pembayaran awal
- **Google Docs:** laporan grup per divisi ditambahkan ke tab `ARENA` atau `VAULT`
- **`report/`:** salinan laporan dalam bentuk file teks
- Judul, sapaan, pembuka, dan ketentuan pembayaran laporan bisa diatur lewat `.env`

### 9. Keamanan dan konfigurasi
- Login Instagram memakai cookies sesi, bukan password
- Akses Google memakai akun layanan (*service account*)
- Semua data pribadi (link sheet, nama dokumen, hashtag, teks laporan, rekening pembayaran) dipisahkan ke `.env`
- Deteksi sesi Instagram yang tidak valid (halaman login atau *checkpoint*) dengan pesan jelas
- Antarmuka Instagram berbahasa Inggris maupun Indonesia dikenali

## Struktur kode

| File | Peran |
|---|---|
| `main.py` | Setup harian, pemeriksaan, review, ekspor, dan titik masuk |
| `core/browser.py` | Meluncurkan Chrome dengan cookies tersimpan |
| `core/sheets.py` | Menulis baris denda ke Google Sheets |
| `core/docs.py` | Menambahkan laporan ke tab Google Docs |
| `core/config_loader.py` | Menyimpan dan memuat setup harian |

## Ide pengembangan lanjutan

- Memecah `main.py` (sekitar 4.000 baris) menjadi beberapa modul: setup, checker Story, checker Feed, review, ekspor