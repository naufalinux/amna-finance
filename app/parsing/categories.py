"""Category taxonomy and keyword inference.

Keywords are Indonesian-first with common English and brand names mixed in,
since that is how the messages actually arrive.
"""

import re

UNCATEGORIZED = "Uncategorized"

CATEGORIES: dict[str, tuple[str, ...]] = {
    "Food & Drink": (
        "makan", "makanan", "sarapan", "brunch", "lunch", "siang", "dinner",
        "malam", "jajan", "snack", "cemilan", "kopi", "coffee", "kafe", "cafe",
        "teh", "boba", "starbucks", "kopken", "janji", "fore", "tomoro",
        "nasi", "ayam", "bakso", "mie", "soto", "sate", "padang", "warteg",
        "warung", "resto", "restoran", "gofood", "grabfood", "shopeefood",
        "mcd", "kfc", "burger", "pizza", "roti", "es", "jus", "minum",
        "martabak", "gorengan", "seblak", "geprek", "katsu", "ramen", "sushi",
    ),
    "Transport": (
        "grab", "gojek", "gocar", "goride", "gorides", "maxim", "ojek", "ojol",
        "taksi", "taxi", "bluebird", "angkot", "busway", "transjakarta", "mrt",
        "lrt", "krl", "kereta", "commuter", "bensin", "pertamax", "pertalite",
        "solar", "isi", "spbu", "parkir", "parking", "tol", "etoll", "e-toll",
        "damri", "travel", "pesawat", "tiket", "flight", "garuda", "lion",
        "bandara", "servis", "montir", "oli", "ban",
    ),
    "Groceries": (
        "belanja", "groceries", "grocery", "sayur", "buah", "daging", "telur",
        "beras", "minyak", "gula", "susu", "pasar", "supermarket", "indomaret",
        "alfamart", "alfamidi", "superindo", "hypermart", "carrefour", "transmart",
        "ranch", "hero", "lotte", "sembako", "galon", "gas", "elpiji",
    ),
    "Bills & Utilities": (
        "listrik", "pln", "token", "air", "pdam", "internet", "wifi", "indihome",
        "biznet", "firstmedia", "pulsa", "paket", "kuota", "telkomsel", "xl",
        "indosat", "smartfren", "tagihan", "iuran", "sewa", "kontrakan",
        "kos", "kost", "netflix", "spotify", "youtube", "disney", "vidio",
        "langganan", "subscription", "cicilan", "kartu", "asuransi", "bpjs",
    ),
    "Health": (
        "obat", "apotek", "apotik", "kimia", "guardian", "dokter", "klinik", "rs", "puskesmas", "vitamin", "medical", "checkup", "gigi",
        "lab", "vaksin", "terapi", "psikolog", "kacamata", "optik",
    ),
    "Shopping": (
        "baju", "celana", "sepatu", "sandal", "tas", "kaos", "jaket", "kemeja",
        "uniqlo", "zara", "hnm", "shopee", "tokopedia", "lazada", "tiktok",
        "bukalapak", "blibli", "olshop", "elektronik", "hp", "laptop",
        "charger", "kabel", "headset", "skincare", "kosmetik", "parfum",
        "sociolla", "salon", "potong", "barber", "cukur", "laundry",
    ),
    "Entertainment": (
        "bioskop", "cinema", "xxi", "cgv", "film", "nonton", "konser",
        "game", "steam", "diamond", "karaoke",
        "wisata", "liburan", "hotel", "penginapan", "villa", "gym", "fitness",
        "renang", "futsal", "badminton", "komik",
    ),
    "Home": (
        "perabot", "furnitur", "ikea", "informa", "ace", "kasur",
        "bantal", "sprei", "sapu", "pel", "detergen", "sabun", "shampoo",
        "tisu", "pembersih", "lampu", "paku", "cat", "tukang", "perbaikan",
    ),
    "Education": (
        "kursus", "les", "kuliah", "spp", "sekolah", "buku", "seminar",
        "workshop", "pelatihan", "sertifikasi", "udemy", "coursera",
    ),
    "Fees & Transfers": (
        "admin", "biaya", "transfer", "tf", "topup", "saldo", "gopay",
        "ovo", "dana", "shopeepay", "linkaja", "pajak", "denda", "zakat",
        "sedekah", "infaq", "donasi", "amal", "kado", "hadiah", "angpao",
    ),
}

# Longest keyword first so "isi bensin" beats a bare "isi".
_KEYWORD_INDEX: list[tuple[str, str]] = sorted(
    ((kw, cat) for cat, kws in CATEGORIES.items() for kw in kws),
    key=lambda pair: len(pair[0]),
    reverse=True,
)

_WORD_RE = re.compile(r"[a-z0-9]+")


def infer(note: str, default: str = UNCATEGORIZED) -> str:
    """Map a free-text note onto a category, or `default` if nothing matches."""
    if not note:
        return default
    words = set(_WORD_RE.findall(note.lower()))
    if not words:
        return default
    for keyword, category in _KEYWORD_INDEX:
        if keyword in words:
            return category
    return default


def normalize(category: str | None) -> str:
    """Accept an LLM-supplied category, snapping near-misses onto the taxonomy."""
    if not category:
        return UNCATEGORIZED
    cleaned = category.strip()
    lowered = cleaned.lower()
    for known in CATEGORIES:
        if lowered == known.lower():
            return known
    # "Food", "food and drink", "Makanan" -> "Food & Drink"
    for known in CATEGORIES:
        head = known.lower().split(" &")[0]
        if lowered.startswith(head) or head.startswith(lowered):
            return known
    return infer(cleaned, default=cleaned.title())
