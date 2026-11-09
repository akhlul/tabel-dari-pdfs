"""Library categories: a path of names, e.g. ["Statistik Ekonomi", "Transportasi"] (any depth).

BPS imports are categorised automatically with the BPS subject classification (CSA, from
`list/model/subcatcsa` + `list/model/subjectcsa` of the BPS Web API, the same for every domain):
  category = the CSA subject category, sub-category = the CSA subject.
Publications carry `id_subject_csa`, press releases `subcsa_id`. Many regional publications have no
subject, so the title is matched against keywords instead. Uploads start uncategorised ([]).
"""

from __future__ import annotations

import re

SUBCAT = {
    514: "Statistik Demografi dan Sosial",
    515: "Statistik Ekonomi",
    516: "Statistik Lingkungan Hidup dan Multi-domain",
}

# sub_id -> (subcat_id, subject)
SUBJECT: dict[int, tuple[int, str]] = {
    519: (514, "Kependudukan dan Migrasi"),
    520: (514, "Tenaga Kerja"),
    521: (514, "Pendidikan"),
    522: (514, "Kesehatan"),
    523: (514, "Konsumsi dan Pendapatan"),
    524: (514, "Perlindungan Sosial"),
    525: (514, "Pemukiman dan Perumahan"),
    526: (514, "Hukum dan Kriminal"),
    527: (514, "Budaya"),
    528: (514, "Aktivitas Politik dan Komunitas Lainnya"),
    529: (514, "Penggunaan Waktu"),
    530: (515, "Statistik Makroekonomi"),
    531: (515, "Neraca Ekonomi"),
    532: (515, "Statistik Bisnis"),
    533: (515, "Statistik sektoral"),
    534: (515, "Keuangan Pemerintah, Fiskal dan Statistik Sektor Publik"),
    535: (515, "Perdagangan Internasional dan Neraca Pembayaran"),
    536: (515, "Harga-Harga"),
    537: (515, "Biaya Tenaga Kerja"),
    538: (515, "Ilmu Pengetahuan, Teknologi, dan Inovasi"),
    557: (515, "Pertanian, Kehutanan, Perikanan"),
    558: (515, "Energi"),
    559: (515, "Pertambangan, Manufaktur, Konstruksi"),
    560: (515, "Transportasi"),
    561: (515, "Pariwisata"),
    562: (515, "Perbankan, Asuransi dan Finansial"),
    539: (516, "Lingkungan"),
    540: (516, "Statistik Regional dan Statistik Area Kecil"),
    541: (516, "Statistik dan Indikator Multi-Domain"),
    542: (516, "Buku Tahunan dan Ringkasan Sejenis"),
    563: (516, "Kondisi Tempat Tinggal, Kemiskinan, dan Permasalahan Sosial Lintas Sektor"),
    564: (516, "Gender dan Kelompok Populasi Khusus"),
    565: (516, "Masyarakat Informasi"),
    566: (516, "Globalisasi"),
    567: (516, "Indikator Millenium Development Goals (MDGs)"),
    568: (516, "Pembangunan Berkelanjutan"),
    569: (516, "Kewiraswastaan"),
}

OTHER = "Lainnya"

# title keywords -> CSA subject, most specific first (the first match wins)
_KEYWORDS: list[tuple[str, int]] = [
    (r"dalam angka|statistik daerah|statistik indonesia|ringkasan|buku saku", 542),
    (r"perdagangan luar negeri|ekspor|impor|neraca perdagangan|neraca pembayaran", 535),
    (r"makro", 530),
    (r"pdrb|pdb|produk domestik|pertumbuhan ekonomi|ekonomi .{0,40}tumbuh|neraca", 531),
    (r"inflasi|harga|ihk|nilai tukar petani|ntp|ntn", 536),
    (r"upah|gaji", 537),
    (r"tenaga kerja|ketenagakerjaan|angkatan kerja|pengangguran|sakernas", 520),
    (r"perikanan|ikan|pertanian|padi|jagung|palawija|tanaman|perkebunan|peternakan|ternak|kehutanan|hutan|"
     r"hortikultura|sayur|buah|pangan|petani|ubinan", 557),
    (r"pariwisata|wisata|hotel|akomodasi|wisatawan", 561),
    (r"transportasi|angkutan|pelabuhan|bandara|penerbangan|kereta|kendaraan", 560),
    (r"energi|listrik|bbm|migas", 558),
    (r"industri|manufaktur|konstruksi|pertambangan|tambang|penggalian", 559),
    (r"perbankan|bank|asuransi|finansial|moneter|kredit", 562),
    (r"keuangan (?:pemerintah|daerah|desa)|apbd|apbn|fiskal|pajak|statistik keuangan", 534),
    (r"kemiskinan|miskin|kesejahteraan|pembangunan manusia|\bipm\b|ketimpangan|gini", 563),
    (r"penduduk|kependudukan|demografi|migrasi|fertilitas|kelahiran|kematian|proyeksi", 519),
    (r"pendidikan|sekolah|literasi", 521),
    (r"kesehatan|gizi|stunting", 522),
    (r"konsumsi|pengeluaran|pendapatan|susenas", 523),
    (r"perlindungan sosial|jaminan sosial|bantuan sosial", 524),
    (r"perumahan|permukiman|pemukiman|rumah tangga", 525),
    (r"kriminal|kejahatan|hukum", 526),
    (r"gender|perempuan|\banak\b|pemuda|lansia|lanjut usia|disabilitas", 564),
    (r"teknologi informasi|\btik\b|internet|telekomunikasi|digital", 565),
    (r"lingkungan|iklim|bencana|cuaca", 539),
    (r"\bdesa\b|kecamatan|podes|potensi desa|master file|wilayah|kabupaten|kota\b", 540),
    (r"perusahaan|usaha|umkm|\bukm\b|koperasi|direktori|perdagangan|distribusi", 532),
    (r"pembangunan berkelanjutan|sdgs?\b", 568),
    (r"\bmdgs?\b", 567),
    (r"politik|pemilu|demokrasi|pemerintahan", 528),
    (r"indikator|statistik", 541),
]
_KW = [(re.compile(p, re.I), sid) for p, sid in _KEYWORDS]


def subject_path(sub_id: int | str | None) -> list[str] | None:
    try:
        cat, name = SUBJECT[int(sub_id)]  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError):
        return None
    return [SUBCAT[cat], name]


def guess_from_title(title: str) -> list[str] | None:
    for rx, sid in _KW:
        if rx.search(title or ""):
            return subject_path(sid)
    return None


def for_bps(meta: dict, title: str = "") -> list[str]:
    """Category path of a BPS item: its CSA subject, else a title guess, else 'Lainnya'."""
    for sid in meta.get("subject_ids") or []:
        p = subject_path(sid)
        if p:
            return p
    return guess_from_title(title or meta.get("title") or "") or [OTHER]


def for_document(d: dict) -> list[str]:
    return for_bps(d.get("meta") or {}, d.get("title", "")) if d.get("source") == "bps" else []


def clean(path: list[str]) -> list[str]:
    """User input: trimmed names, no empty levels, at most 6 levels."""
    return [re.sub(r"\s+", " ", p).strip()[:120] for p in path if p and p.strip()][:6]
