# -*- coding: utf-8 -*-
# AkademikRadar Scraper — ilan.gov.tr API Edition
#
# Listing:  POST https://www.ilan.gov.tr/api/api/services/app/Ad/AdsByFilter
#           Body: {"keys":{"txv":[73]},"skipCount":0,"maxResultCount":50}
# Detail:   GET  https://www.ilan.gov.tr/api/api/services/app/AdDetail/GetAdDetail?id={id}

import json, logging, os, re, time
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup

# ilan.gov.tr has SSL cert issues on some runners (missing intermediate CA).
# Disable verification and suppress the warning.
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
VERIFY_SSL = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("scraper")

API_BASE    = "https://www.ilan.gov.tr/api/api/services/app"
LISTING_URL = f"{API_BASE}/Ad/AdsByFilter"
DETAIL_URL  = f"{API_BASE}/AdDetail/GetAdDetail"
TAX_ID      = 73

UNIVERSITY_LIST_URL   = "https://raw.githubusercontent.com/sametabbak/AkademikRadarFiltreListesi/refs/heads/main/TurkishUniversityList"
UNIVERSITY_CACHE_FILE = "university_list_cache.json"

_output_dir = os.environ.get("OUTPUT_DIR", "").strip()
OUTPUT_FILE = os.path.join(_output_dir, "ilanlar.json") if _output_dir else "ilanlar.json"

PAGE_SIZE           = 50
TIMEOUT             = 20
MAX_RUNTIME_SECONDS = 20 * 60
REQUEST_DELAY       = 0.5

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json-patch+json",
    "Accept-Language": "tr-TR,tr;q=0.9",
}
_session = requests.Session()
_session.headers.update(HEADERS)
_start_time = time.time()

# ── Academic title constants ───────────────────────────────────────────────────
# ── Academic title groups ─────────────────────────────────────────────────────
# Öğretim Üyesi subgroup — the three faculty ranks
OGRETIM_UYESI_TITLES = ["PROFESÖR", "DOÇENT", "DR. ÖĞR. ÜYESİ"]

# All individual academic titles
ACADEMIC_TITLES = [
    # Öğretim Üyesi (faculty)
    "PROFESÖR",
    "DOÇENT",
    "DR. ÖĞR. ÜYESİ",
    # Öğretim Görevlisi
    "ÖĞRETİM GÖREVLİSİ",
    # Araştırma Görevlisi
    "ARAŞTIRMA GÖREVLİSİ",
    # Diğer
    "UZMAN",
    "OKUTMAN",
    "ÇEVİRİCİ",
    "EĞİTİM-ÖĞRETİM PLANLAMACISI",
]

# Comprehensive alias map.
# "__OGRETIM_UYESI__" is a sentinel meaning the position is open to all three
# faculty ranks → stored as "PROFESÖR / DOÇENT / DR. ÖĞR. ÜYESİ"
TITLE_ALIASES = {
    # ── Öğretim Üyesi (generic — all three ranks) ─────────────────────────
    "ÖĞRETİM ÜYELERİ":                    "__OGRETIM_UYESI__",
    "ÖĞRETİM ÜYESİ ALIMI":              "__OGRETIM_UYESI__",
    "ÖĞRETİM ÜYESİ":                    "__OGRETIM_UYESI__",
    "ÖĞRETİM ÜYESİ (PROF./ DOÇ./ DR.)":  "__OGRETIM_UYESI__",

    # ── PROFESÖR ──────────────────────────────────────────────────────────
    "PROFESÖR DR.":                        "PROFESÖR",
    "PROFESÖR":                            "PROFESÖR",
    "PROF. DR.":                          "PROFESÖR",
    "PROF.DR.":                           "PROFESÖR",
    "PROF.":                              "PROFESÖR",

    # ── DOÇENT ────────────────────────────────────────────────────────────
    "DOÇENT DR.":                       "DOÇENT",
    "DOÇ. DR.":                         "DOÇENT",
    "DOÇ.DR.":                          "DOÇENT",
    "DOÇ.":                             "DOÇENT",

    # ── DR. ÖĞR. ÜYESİ ───────────────────────────────────────────────────
    "YARDIMCI DOÇENT DR.":              "DR. ÖĞR. ÜYESİ",
    "YARDIMCI DOÇENT":                 "DR. ÖĞR. ÜYESİ",
    "YRD. DOÇ. DR.":                   "DR. ÖĞR. ÜYESİ",
    "YRD.DOÇ.DR.":                     "DR. ÖĞR. ÜYESİ",
    "YRD. DOÇ.":                       "DR. ÖĞR. ÜYESİ",
    "DOKTOR ÖĞRETİM ÜYESİ":          "DR. ÖĞR. ÜYESİ",
    "DR. ÖĞRETİM ÜYESİ":             "DR. ÖĞR. ÜYESİ",
    "DR.ÖĞRETİM ÜYESİ":              "DR. ÖĞR. ÜYESİ",
    "DR ÖĞRETİM ÜYESİ":              "DR. ÖĞR. ÜYESİ",
    "DR. ÖĞR. ÜYESİ":                    "DR. ÖĞR. ÜYESİ",
    "DR.ÖĞR.ÜYESİ":                      "DR. ÖĞR. ÜYESİ",
    "DR ÖĞR ÜYESİ":                      "DR. ÖĞR. ÜYESİ",
    "DR.ÖĞR. ÜYESİ":                     "DR. ÖĞR. ÜYESİ",
    "DR. ÖĞR.ÜYESİ":                     "DR. ÖĞR. ÜYESİ",
    "DR. ÖĞR.":                        "DR. ÖĞR. ÜYESİ",

    # ── ÖĞRETİM GÖREVLİSİ ────────────────────────────────────────────────
    "ÖĞRETİM GÖREVLİSİ DR.":       "ÖĞRETİM GÖREVLİSİ",
    "ÖĞR. GÖR. DR.":                  "ÖĞRETİM GÖREVLİSİ",
    "ÖĞR. GÖR.":                       "ÖĞRETİM GÖREVLİSİ",
    "ÖĞR.GÖR.":                        "ÖĞRETİM GÖREVLİSİ",
    "ÖĞRETİM GÖR.":               "ÖĞRETİM GÖREVLİSİ",

    # ── ARAŞTIRMA GÖREVLİSİ ───────────────────────────────────────────────
    "ARAŞTIRMA GÖREVLİSİ DR.":    "ARAŞTIRMA GÖREVLİSİ",
    "ARŞ. GÖR. DR.":                  "ARAŞTIRMA GÖREVLİSİ",
    "ARŞ. GÖR.":                       "ARAŞTIRMA GÖREVLİSİ",
    "ARŞ.GÖR.":                        "ARAŞTIRMA GÖREVLİSİ",
    "ARAŞTIRMA GÖR.":                 "ARAŞTIRMA GÖREVLİSİ",

    # ── UZMAN ─────────────────────────────────────────────────────────────
    "UZMAN DR.":                          "UZMAN",

    # ── OKUTMAN ───────────────────────────────────────────────────────────
    "BAŞ OKUTMAN":                       "OKUTMAN",
    "BAŞOKUTMAN":                        "OKUTMAN",

    # ── ÇEVİRİCİ ──────────────────────────────────────────────────────────
    "BAŞ ÇEVİRİCİ":                     "ÇEVİRİCİ",

    # ── EĞİTİM-ÖĞRETİM PLANLAMACISI ──────────────────────────────────────
    "EĞİTİM ÖĞRETİM PLANLAMACISI":  "EĞİTİM-ÖĞRETİM PLANLAMACISI",
}

ALES_EXEMPT_TITLES = {"PROFESÖR", "DOÇENT"}

# ── Turkish helpers ───────────────────────────────────────────────────────────────
def tr_upper(s: str) -> str:
    return s.replace("i", "İ").replace("ı", "I").upper()

def normalize_for_match(s: str) -> str:
    return (
        s.replace("İ", "I").replace("ı", "I").replace("i", "I")
         .replace("ğ", "g").replace("Ğ", "G").replace("ş", "s").replace("Ş", "S")
         .replace("ç", "c").replace("Ç", "C").replace("ö", "o").replace("Ö", "O")
         .replace("ü", "u").replace("Ü", "U").replace("â", "a").replace("Â", "A")
         .upper()
    )

def clean(s) -> str:
    if not s: return ""
    return re.sub(r"\s+", " ", str(s)).strip()

def budget_ok() -> bool:
    return (time.time() - _start_time) < MAX_RUNTIME_SECONDS

# ── Title helpers ───────────────────────────────────────────────────────────────
def extract_titles_from_cell(raw: str) -> list:
    """
    Extract all academic titles from a cell value.
    Handles:
    - Combined cells: "Profesör / Doçent / Dr. Öğr. Üyesi" → 3 titles
    - Generic "Öğretim Üyesi" → expands to all three faculty ranks
    - Abbreviations: "Doç. Dr.", "Arş. Gör." etc.
    """
    found = []
    parts = re.split(r"[/,;]|\bve\b|\bveya\b", raw, flags=re.IGNORECASE)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        part_up = tr_upper(part)
        matched = None
        # Check aliases first — longer aliases checked before shorter ones
        for alias in sorted(TITLE_ALIASES, key=len, reverse=True):
            if tr_upper(alias) in part_up:
                matched = TITLE_ALIASES[alias]
                break
        # Sentinel: "Öğretim Üyesi" expands to all three faculty ranks
        if matched == "__OGRETIM_UYESI__":
            for t in OGRETIM_UYESI_TITLES:
                if t not in found:
                    found.append(t)
            continue
        if not matched:
            # Direct match against known titles
            for t in ACADEMIC_TITLES:
                if tr_upper(t) in part_up:
                    matched = t
                    break
        if matched and matched not in found:
            found.append(matched)
    return found

def is_academic(title: str) -> bool:
    if title in ACADEMIC_TITLES: return True
    return any(p.strip() in ACADEMIC_TITLES for p in re.split(r"[/]", title))

# ── ALES / Language ───────────────────────────────────────────────────────────────────
def extract_ales(text: str, title: str = "") -> dict:
    r = {"alesRequired": False, "alesScore": None, "alesType": None}
    if title in ALES_EXEMPT_TITLES: return r
    up = tr_upper(text)
    if "ALES" not in up: return r
    r["alesRequired"] = True
    m = re.search(r"ALES[^0-9]{0,30}(\d{2,3})", up)
    if m: r["alesScore"] = int(m.group(1))
    for t in ["SAY", "SÖZ", "EA", "SAYISAL", "SÖZEL", "EŞİT AĞIRLIK"]:
        if t in up: r["alesType"] = t; break
    return r

def extract_language(text: str, title: str = "") -> dict:
    r = {"foreignLanguageRequired": False, "foreignLanguageScore": None, "foreignLanguageExam": None}
    up = tr_upper(text)
    for kw in ["YDS", "YÖKDİL", "YABANCI DİL"]:
        if tr_upper(kw) in up:
            r["foreignLanguageRequired"] = True
            break
    if not r["foreignLanguageRequired"]: return r
    m = re.search(r"(?:YDS|YÖKDİL|YABANCI\s+DİL)[^0-9]{0,30}(\d{2,3})", up)
    if m: r["foreignLanguageScore"] = int(m.group(1))
    for exam in ["YDS", "YÖKDİL", "TOEFL", "IELTS"]:
        if tr_upper(exam) in up: r["foreignLanguageExam"] = exam; break
    return r

# ── Deadline ───────────────────────────────────────────────────────────────────────────
def extract_deadline(text: str, publish_date) -> str | None:
    m = re.search(r"son\s+başvuru\s+tarih\w*[:\s]*(\d{1,2})[./](\d{1,2})[./](\d{4})", text, re.IGNORECASE)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), 23, 59, tzinfo=timezone.utc).isoformat()
        except ValueError: pass
    for c in re.finditer(r"başvuru\s+tarih\w*[:\s]*(\d{1,2})[./](\d{1,2})[./](\d{4})", text, re.IGNORECASE):
        try:
            cd = datetime(int(c.group(3)), int(c.group(2)), int(c.group(1)), tzinfo=timezone.utc)
            if publish_date is None or cd > publish_date:
                return cd.replace(hour=23, minute=59).isoformat()
        except ValueError: pass
    m = re.search(r"tarihinden\s+itibaren\s+(\d{1,2})\s+gün", text, re.IGNORECASE)
    if m and publish_date:
        return (publish_date + timedelta(days=int(m.group(1)))).replace(hour=23, minute=59).isoformat()
    return None

# ── University list ───────────────────────────────────────────────────────────────
FALLBACK_UNIVERSITY_LIST = [
      { "Name": "ADANA ALPARSLAN TÜRKEŞ BİLİM VE TEKNOLOJİ ÜNİVERSİTESİ", "City": "Adana", "Type": "Devlet" },
      { "Name": "ÇUKUROVA ÜNİVERSİTESİ", "City": "Adana", "Type": "Devlet" },
      { "Name": "ADIYAMAN ÜNİVERSİTESİ", "City": "Adıyaman", "Type": "Devlet" },
      { "Name": "AFYON KOCATEPE ÜNİVERSİTESİ", "City": "Afyonkarahisar", "Type": "Devlet" },
      { "Name": "AFYONKARAHİSAR SAĞLIK BİLİMLERİ ÜNİVERSİTESİ", "City": "Afyonkarahisar", "Type": "Devlet" },
      { "Name": "AĞRI İBRAHİM ÇEÇEN ÜNİVERSİTESİ", "City": "Ağrı", "Type": "Devlet" },
      { "Name": "AKSARAY ÜNİVERSİTESİ", "City": "Aksaray", "Type": "Devlet" },
      { "Name": "AMASYA ÜNİVERSİTESİ", "City": "Amasya", "Type": "Devlet" },
      { "Name": "ANKARA BİLİM ÜNİVERSİTESİ", "City": "Ankara", "Type": "Vakıf" },
      { "Name": "ANKARA HACI BAYRAM VELİ ÜNİVERSİTESİ", "City": "Ankara", "Type": "Devlet" },
      { "Name": "ANKARA MEDİPOL ÜNİVERSİTESİ", "City": "Ankara", "Type": "Vakıf" },
      { "Name": "ANKARA SOSYAL BİLİMLER ÜNİVERSİTESİ", "City": "Ankara", "Type": "Devlet" },
      { "Name": "ANKARA ÜNİVERSİTESİ", "City": "Ankara", "Type": "Devlet" },
      { "Name": "ANKARA YILDIRIM BEYAZIT ÜNİVERSİTESİ", "City": "Ankara", "Type": "Devlet" },
      { "Name": "ATILIM ÜNİVERSİTESİ", "City": "Ankara", "Type": "Vakıf" },
      { "Name": "BAŞKENT ÜNİVERSİTESİ", "City": "Ankara", "Type": "Vakıf" },
      { "Name": "ÇANKAYA ÜNİVERSİTESİ", "City": "Ankara", "Type": "Vakıf" },
      { "Name": "GAZİ ÜNİVERSİTESİ", "City": "Ankara", "Type": "Devlet" },
      { "Name": "HACETTEPE ÜNİVERSİTESİ", "City": "Ankara", "Type": "Devlet" },
      { "Name": "İHSAN DOĞRAMACI BİLKENT ÜNİVERSİTESİ", "City": "Ankara", "Type": "Vakıf" },
      { "Name": "LOKMAN HEKİM ÜNİVERSİTESİ", "City": "Ankara", "Type": "Vakıf" },
      { "Name": "ORTA DOĞU TEKNİK ÜNİVERSİTESİ", "City": "Ankara", "Type": "Devlet" },
      { "Name": "OSTİM TEKNİK ÜNİVERSİTESİ", "City": "Ankara", "Type": "Vakıf" },
      { "Name": "TED ÜNİVERSİTESİ", "City": "Ankara", "Type": "Vakıf" },
      { "Name": "TOBB EKONOMİ VE TEKNOLOJİ ÜNİVERSİTESİ", "City": "Ankara", "Type": "Vakıf" },
      { "Name": "UFUK ÜNİVERSİTESİ", "City": "Ankara", "Type": "Vakıf" },
      { "Name": "YÜKSEK İHTİSAS ÜNİVERSİTESİ", "City": "Ankara", "Type": "Vakıf" },
      { "Name": "AKDENİZ ÜNİVERSİTESİ", "City": "Antalya", "Type": "Devlet" },
      { "Name": "ALANYA ALAADDİN KEYKUBAT ÜNİVERSİTESİ", "City": "Antalya", "Type": "Devlet" },
      { "Name": "ANTALYA BİLİM ÜNİVERSİTESİ", "City": "Antalya", "Type": "Vakıf" },
      { "Name": "ARDAHAN ÜNİVERSİTESİ", "City": "Ardahan", "Type": "Devlet" },
      { "Name": "ARTVİN ÇORUH ÜNİVERSİTESİ", "City": "Artvin", "Type": "Devlet" },
      { "Name": "AYDIN ADNAN MENDERES ÜNİVERSİTESİ", "City": "Aydın", "Type": "Devlet" },
      { "Name": "BALIKESİR ÜNİVERSİTESİ", "City": "Balıkesir", "Type": "Devlet" },
      { "Name": "BANDIRMA ONYEDİ EYLÜL ÜNİVERSİTESİ", "City": "Balıkesir", "Type": "Devlet" },
      { "Name": "BARTIN ÜNİVERSİTESİ", "City": "Bartın", "Type": "Devlet" },
      { "Name": "BATMAN ÜNİVERSİTESİ", "City": "Batman", "Type": "Devlet" },
      { "Name": "BAYBURT ÜNİVERSİTESİ", "City": "Bayburt", "Type": "Devlet" },
      { "Name": "BİLECİK ŞEYH EDEBALİ ÜNİVERSİTESİ", "City": "Bilecik", "Type": "Devlet" },
      { "Name": "BİNGÖL ÜNİVERSİTESİ", "City": "Bingöl", "Type": "Devlet" },
      { "Name": "BİTLİS EREN ÜNİVERSİTESİ", "City": "Bitlis", "Type": "Devlet" },
      { "Name": "BOLU ABANT İZZET BAYSAL ÜNİVERSİTESİ", "City": "Bolu", "Type": "Devlet" },
      { "Name": "BURDUR MEHMET AKİF ERSOY ÜNİVERSİTESİ", "City": "Burdur", "Type": "Devlet" },
      { "Name": "BURSA TEKNİK ÜNİVERSİTESİ", "City": "Bursa", "Type": "Devlet" },
      { "Name": "BURSA ULUDAĞ ÜNİVERSİTESİ", "City": "Bursa", "Type": "Devlet" },
      { "Name": "MUDANYA ÜNİVERSİTESİ", "City": "Bursa", "Type": "Vakıf" },
      { "Name": "ÇANAKKALE ONSEKİZ MART ÜNİVERSİTESİ", "City": "Çanakkale", "Type": "Devlet" },
      { "Name": "ÇANKIRI KARATEKİN ÜNİVERSİTESİ", "City": "Çankırı", "Type": "Devlet" },
      { "Name": "HİTİT ÜNİVERSİTESİ", "City": "Çorum", "Type": "Devlet" },
      { "Name": "PAMUKKALE ÜNİVERSİTESİ", "City": "Denizli", "Type": "Devlet" },
      { "Name": "DİCLE ÜNİVERSİTESİ", "City": "Diyarbakır", "Type": "Devlet" },
      { "Name": "DÜZCE ÜNİVERSİTESİ", "City": "Düzce", "Type": "Devlet" },
      { "Name": "TRAKYA ÜNİVERSİTESİ", "City": "Edirne", "Type": "Devlet" },
      { "Name": "FIRAT ÜNİVERSİTESİ", "City": "Elazığ", "Type": "Devlet" },
      { "Name": "ERZİNCAN BİNALİ YILDIRIM ÜNİVERSİTESİ", "City": "Erzincan", "Type": "Devlet" },
      { "Name": "ATATÜRK ÜNİVERSİTESİ", "City": "Erzurum", "Type": "Devlet" },
      { "Name": "ERZURUM TEKNİK ÜNİVERSİTESİ", "City": "Erzurum", "Type": "Devlet" },
      { "Name": "ANADOLU ÜNİVERSİTESİ", "City": "Eskişehir", "Type": "Devlet" },
      { "Name": "ESKİŞEHİR OSMANGAZİ ÜNİVERSİTESİ", "City": "Eskişehir", "Type": "Devlet" },
      { "Name": "ESKİŞEHİR TEKNİK ÜNİVERSİTESİ", "City": "Eskişehir", "Type": "Devlet" },
      { "Name": "GAZİANTEP İSLAM BİLİM VE TEKNOLOJİ ÜNİVERSİTESİ", "City": "Gaziantep", "Type": "Devlet" },
      { "Name": "GAZİANTEP ÜNİVERSİTESİ", "City": "Gaziantep", "Type": "Devlet" },
      { "Name": "HASAN KALYONCU ÜNİVERSİTESİ", "City": "Gaziantep", "Type": "Vakıf" },
      { "Name": "SANKO ÜNİVERSİTESİ", "City": "Gaziantep", "Type": "Vakıf" },
      { "Name": "GİRESUN ÜNİVERSİTESİ", "City": "Giresun", "Type": "Devlet" },
      { "Name": "GÜMÜŞHANE ÜNİVERSİTESİ", "City": "Gümüşhane", "Type": "Devlet" },
      { "Name": "HAKKARİ ÜNİVERSİTESİ", "City": "Hakkari", "Type": "Devlet" },
      { "Name": "İSKENDERUN TEKNİK ÜNİVERSİTESİ", "City": "Hatay", "Type": "Devlet" },
      { "Name": "HATAY MUSTAFA KEMAL ÜNİVERSİTESİ", "City": "Hatay", "Type": "Devlet" },
      { "Name": "IĞDIR ÜNİVERSİTESİ", "City": "Iğdır", "Type": "Devlet" },
      { "Name": "ISPARTA UYGULAMALI BİLİMLER ÜNİVERSİTESİ", "City": "Isparta", "Type": "Devlet" },
      { "Name": "SÜLEYMAN DEMİREL ÜNİVERSİTESİ", "City": "Isparta", "Type": "Devlet" },
      { "Name": "ACIBADEM MEHMET ALİ AYDINLAR ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "ALTINBAŞ ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "BAHÇEŞEHİR ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "BEYKOZ ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "BEZM-İ ÂLEM VAKIF ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "BİRUNİ ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "BOĞAZİÇİ ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Devlet" },
      { "Name": "DEMİROĞLU BİLİM ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "DOĞUŞ ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "FATİH SULTAN MEHMET VAKIF ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "FENERBAHÇE ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "GALATASARAY ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Devlet" },
      { "Name": "HALİÇ ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "IŞIK ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İBN HALDUN ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İSTANBUL 29 MAYIS ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İSTANBUL AREL ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İSTANBUL ATLAS ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İSTANBUL AYDIN ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İSTANBUL BEYKENT ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İSTANBUL BİLGİ ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İSTANBUL ESENYURT ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İSTANBUL GALATA ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İSTANBUL GEDİK ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İSTANBUL GELİŞİM ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İSTANBUL KENT ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İSTANBUL KÜLTÜR ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İSTANBUL MEDENİYET ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Devlet" },
      { "Name": "İSTANBUL MEDİPOL ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İSTANBUL OKAN ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İSTANBUL RUMELİ ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İSTANBUL SABAHATTİN ZAİM ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İSTANBUL SAĞLIK VE TEKNOLOJİ ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İSTANBUL TİCARET ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İSTANBUL TEKNİK ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Devlet" },
      { "Name": "İSTANBUL TOPKAPI ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İSTANBUL ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Devlet" },
      { "Name": "İSTANBUL ÜNİVERSİTESİ-CERRAHPAŞA", "City": "İstanbul", "Type": "Devlet" },
      { "Name": "İSTANBUL YENİ YÜZYIL ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "İSTİNYE ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "KADİR HAS ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "KOÇ ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "MALTEPE ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "MARMARA ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Devlet" },
      { "Name": "MEF ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "MİMAR SİNAN GÜZEL SANATLAR ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Devlet" },
      { "Name": "İSTANBUL NİŞANTAŞI ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "ÖZYEĞİN ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "PİRİ REİS ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "SABANCI ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "SAĞLIK BİLİMLERİ ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Devlet" },
      { "Name": "TÜRK-ALMAN ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Devlet" },
      { "Name": "TÜRKİYE ULUSLARARASI İSLAM BİLİM VE TEKNOLOJİ ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Devlet" },
      { "Name": "ÜSKÜDAR ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "YEDİTEPE ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Vakıf" },
      { "Name": "YILDIZ TEKNİK ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Devlet" },
      { "Name": "DOKUZ EYLÜL ÜNİVERSİTESİ", "City": "İzmir", "Type": "Devlet" },
      { "Name": "EGE ÜNİVERSİTESİ", "City": "İzmir", "Type": "Devlet" },
      { "Name": "İZMİR BAKIRÇAY ÜNİVERSİTESİ", "City": "İzmir", "Type": "Devlet" },
      { "Name": "İZMİR DEMOKRASİ ÜNİVERSİTESİ", "City": "İzmir", "Type": "Devlet" },
      { "Name": "İZMİR EKONOMİ ÜNİVERSİTESİ", "City": "İzmir", "Type": "Vakıf" },
      { "Name": "İZMİR KÂTİP ÇELEBİ ÜNİVERSİTESİ", "City": "İzmir", "Type": "Devlet" },
      { "Name": "İZMİR TINAZTEPE ÜNİVERSİTESİ", "City": "İzmir", "Type": "Vakıf" },
      { "Name": "İZMİR YÜKSEK TEKNOLOJİ ENSTİTÜSÜ", "City": "İzmir", "Type": "Devlet" },
      { "Name": "YAŞAR ÜNİVERSİTESİ", "City": "İzmir", "Type": "Vakıf" },
      { "Name": "KAHRAMANMARAŞ İSTİKLAL ÜNİVERSİTESİ", "City": "Kahramanmaraş", "Type": "Devlet" },
      { "Name": "KAHRAMANMARAŞ SÜTÇÜ İMAM ÜNİVERSİTESİ", "City": "Kahramanmaraş", "Type": "Devlet" },
      { "Name": "KARABÜK ÜNİVERSİTESİ", "City": "Karabük", "Type": "Devlet" },
      { "Name": "KARAMANOĞLU MEHMETBEY ÜNİVERSİTESİ", "City": "Karaman", "Type": "Devlet" },
      { "Name": "KAFKAS ÜNİVERSİTESİ", "City": "Kars", "Type": "Devlet" },
      { "Name": "KASTAMONU ÜNİVERSİTESİ", "City": "Kastamonu", "Type": "Devlet" },
      { "Name": "ERCİYES ÜNİVERSİTESİ", "City": "Kayseri", "Type": "Devlet" },
      { "Name": "KAYSERİ ÜNİVERSİTESİ", "City": "Kayseri", "Type": "Devlet" },
      { "Name": "NUH NACİ YAZGAN ÜNİVERSİTESİ", "City": "Kayseri", "Type": "Vakıf" },
      { "Name": "KIRIKKALE ÜNİVERSİTESİ", "City": "Kırıkkale", "Type": "Devlet" },
      { "Name": "KIRKLARELİ ÜNİVERSİTESİ", "City": "Kırklareli", "Type": "Devlet" },
      { "Name": "AHİ EVRAN ÜNİVERSİTESİ", "City": "Kırşehir", "Type": "Devlet" },
      { "Name": "KİLİS 7 ARALIK ÜNİVERSİTESİ", "City": "Kilis", "Type": "Devlet" },
      { "Name": "GEBZE TEKNİK ÜNİVERSİTESİ", "City": "Kocaeli", "Type": "Devlet" },
      { "Name": "KOCAELİ SAĞLIK VE TEKNOLOJİ ÜNİVERSİTESİ", "City": "Kocaeli", "Type": "Vakıf" },
      { "Name": "KOCAELİ ÜNİVERSİTESİ", "City": "Kocaeli", "Type": "Devlet" },
      { "Name": "KONYA GIDA VE TARIM ÜNİVERSİTESİ", "City": "Konya", "Type": "Vakıf" },
      { "Name": "KONYA TEKNİK ÜNİVERSİTESİ", "City": "Konya", "Type": "Devlet" },
      { "Name": "KTO KARATAY ÜNİVERSİTESİ", "City": "Konya", "Type": "Vakıf" },
      { "Name": "NECMETTİN ERBAKAN ÜNİVERSİTESİ", "City": "Konya", "Type": "Devlet" },
      { "Name": "SELÇUK ÜNİVERSİTESİ", "City": "Konya", "Type": "Devlet" },
      { "Name": "KÜTAHYA DUMLUPINAR ÜNİVERSİTESİ", "City": "Kütahya", "Type": "Devlet" },
      { "Name": "KÜTAHYA SAĞLIK BİLİMLERİ ÜNİVERSİTESİ", "City": "Kütahya", "Type": "Devlet" },
      { "Name": "İNÖNÜ ÜNİVERSİTESİ", "City": "Malatya", "Type": "Devlet" },
      { "Name": "MALATYA TURGUT ÖZAL ÜNİVERSİTESİ", "City": "Malatya", "Type": "Devlet" },
      { "Name": "MANİSA CELÂL BAYAR ÜNİVERSİTESİ", "City": "Manisa", "Type": "Devlet" },
      { "Name": "MARDİN ARTUKLU ÜNİVERSİTESİ", "City": "Mardin", "Type": "Devlet" },
      { "Name": "MERSİN ÜNİVERSİTESİ", "City": "Mersin", "Type": "Devlet" },
      { "Name": "TARSUS ÜNİVERSİTESİ", "City": "Mersin", "Type": "Devlet" },
      { "Name": "ÇAĞ ÜNİVERSİTESİ", "City": "Mersin", "Type": "Vakıf" },
      { "Name": "MUĞLA SITKI KOÇMAN ÜNİVERSİTESİ", "City": "Muğla", "Type": "Devlet" },
      { "Name": "MUŞ ALPARSLAN ÜNİVERSİTESİ", "City": "Muş", "Type": "Devlet" },
      { "Name": "KAPADOKYA ÜNİVERSİTESİ", "City": "Nevşehir", "Type": "Vakıf" },
      { "Name": "NEVŞEHİR HACI BEKTAŞ VELİ ÜNİVERSİTESİ", "City": "Nevşehir", "Type": "Devlet" },
      { "Name": "NİĞDE ÖMER HALİSDEMİR ÜNİVERSİTESİ", "City": "Niğde", "Type": "Devlet" },
      { "Name": "ORDU ÜNİVERSİTESİ", "City": "Ordu", "Type": "Devlet" },
      { "Name": "OSMANİYE KORKUT ATA ÜNİVERSİTESİ", "City": "Osmaniye", "Type": "Devlet" },
      { "Name": "RECEP TAYYİP ERDOĞAN ÜNİVERSİTESİ", "City": "Rize", "Type": "Devlet" },
      { "Name": "SAKARYA UYGULAMALI BİLİMLER ÜNİVERSİTESİ", "City": "Sakarya", "Type": "Devlet" },
      { "Name": "SAKARYA ÜNİVERSİTESİ", "City": "Sakarya", "Type": "Devlet" },
      { "Name": "ONDOKUZ MAYIS ÜNİVERSİTESİ", "City": "Samsun", "Type": "Devlet" },
      { "Name": "SAMSUN ÜNİVERSİTESİ", "City": "Samsun", "Type": "Devlet" },
      { "Name": "SİİRT ÜNİVERSİTESİ", "City": "Siirt", "Type": "Devlet" },
      { "Name": "SİNOP ÜNİVERSİTESİ", "City": "Sinop", "Type": "Devlet" },
      { "Name": "SİVAS CUMHURİYET ÜNİVERSİTESİ", "City": "Sivas", "Type": "Devlet" },
      { "Name": "SİVAS BİLİM VE TEKNOLOJİ ÜNİVERSİTESİ", "City": "Sivas", "Type": "Devlet" },
      { "Name": "HARRAN ÜNİVERSİTESİ", "City": "Şanlıurfa", "Type": "Devlet" },
      { "Name": "ŞIRNAK ÜNİVERSİTESİ", "City": "Şırnak", "Type": "Devlet" },
      { "Name": "TEKİRDAĞ NAMIK KEMAL ÜNİVERSİTESİ", "City": "Tekirdağ", "Type": "Devlet" },
      { "Name": "TOKAT GAZİOSMANPAŞA ÜNİVERSİTESİ", "City": "Tokat", "Type": "Devlet" },
      { "Name": "KARADENİZ TEKNİK ÜNİVERSİTESİ", "City": "Trabzon", "Type": "Devlet" },
      { "Name": "TRABZON ÜNİVERSİTESİ", "City": "Trabzon", "Type": "Devlet" },
      { "Name": "AVRASYA ÜNİVERSİTESİ", "City": "Trabzon", "Type": "Vakıf" },
      { "Name": "MUNZUR ÜNİVERSİTESİ", "City": "Tunceli", "Type": "Devlet" },
      { "Name": "UŞAK ÜNİVERSİTESİ", "City": "Uşak", "Type": "Devlet" },
      { "Name": "VAN YÜZÜNCÜ YIL ÜNİVERSİTESİ", "City": "Van", "Type": "Devlet" },
      { "Name": "YALOVA ÜNİVERSİTESİ", "City": "Yalova", "Type": "Devlet" },
      { "Name": "YOZGAT BOZOK ÜNİVERSİTESİ", "City": "Yozgat", "Type": "Devlet" },
      { "Name": "ZONGULDAK BÜLENT ECEVİT ÜNİVERSİTESİ", "City": "Zonguldak", "Type": "Devlet" },
      { "Name": "ABDULLAH GÜL ÜNİVERSİTESİ", "City": "Kayseri", "Type": "Devlet" },
      { "Name": "ALANYA ÜNİVERSİTESİ", "City": "Antalya", "Type": "Vakıf" },
      { "Name": "ANKARA MÜZİK VE GÜZEL SANATLAR ÜNİVERSİTESİ", "City": "Ankara", "Type": "Devlet" },
      { "Name": "ANTALYA BELEK ÜNİVERSİTESİ", "City": "Antalya", "Type": "Vakıf" },
      { "Name": "TOROS ÜNİVERSİTESİ", "City": "Mersin", "Type": "Vakıf" },
]

def load_university_list() -> list:
    for attempt in range(1, 4):
        try:
            r = _session.get(UNIVERSITY_LIST_URL, timeout=TIMEOUT, verify=VERIFY_SSL)
            r.raise_for_status()
            data = r.json()
            if data:
                log.info(f"Loaded {len(data)} universities from URL.")
                try:
                    with open(UNIVERSITY_CACHE_FILE, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False)
                except Exception: pass
                return data
        except Exception as e:
            log.warning(f"University list fetch attempt {attempt} failed: {e}")
            if attempt < 3: time.sleep(3)
    if os.path.exists(UNIVERSITY_CACHE_FILE):
        try:
            with open(UNIVERSITY_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data:
                log.info(f"Loaded {len(data)} universities from cache.")
                return data
        except Exception: pass
    log.warning("Using hardcoded university fallback.")
    return FALLBACK_UNIVERSITY_LIST

def match_university(name: str, ulist: list) -> tuple:
    name_norm = normalize_for_match(clean(name))
    name_nsp  = name_norm.replace(" ", "").replace("-", "")
    best, best_len = None, 0
    for uni in ulist:
        u_norm = normalize_for_match(uni["Name"])
        u_nsp  = u_norm.replace(" ", "").replace("-", "")
        matched = u_norm in name_norm or name_norm in u_norm
        if not matched: matched = u_nsp in name_nsp or name_nsp in u_nsp
        if matched and len(u_norm) > best_len:
            best, best_len = uni, len(u_norm)
    if best: return best["Name"], best["City"], best["Type"]
    cleaned = re.sub(r"\s*REKTÖRLÜĞÜ\s*$", "", tr_upper(name.strip()))
    return cleaned, "Bilinmiyor", "Devlet"

# ── Position table parser ───────────────────────────────────────────────────────────────
def parse_positions(content_html: str, full_text: str) -> list:
    soup = BeautifulSoup(content_html, "html.parser")
    positions = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2: continue

        header_idx = None
        col_map: dict = {}

        for ri, row in enumerate(rows):
            cells = [clean(td.get_text()) for td in row.find_all(["th", "td"])]
            if not cells: continue
            cell_up = tr_upper(" ".join(cells))
            has_title = any(tr_upper(k) in cell_up for k in ["UNVAN", "ÜNVAN", "KADRO", "AKADEMİK"])
            has_count = any(tr_upper(k) in cell_up for k in ["ADET", "ADEDİ", "SAYI", "KONTENJAN"])
            if has_title and has_count:
                header_idx = ri
                for ci, cell in enumerate(cells):
                    cu = tr_upper(cell)
                    if any(tr_upper(k) in cu for k in ["FAKÜLTE", "BİRİM"]) and "faculty" not in col_map:
                        col_map["faculty"] = ci
                    elif any(tr_upper(k) in cu for k in ["BÖLÜM", "PROGRAM", "ANABİLİM"]) and "dept" not in col_map:
                        col_map["dept"] = ci
                    elif any(tr_upper(k) in cu for k in ["UNVAN", "ÜNVAN", "KADRO"]) and "title" not in col_map:
                        col_map["title"] = ci
                    elif any(tr_upper(k) in cu for k in ["ADET", "ADEDİ", "SAYI", "KONTENJAN"]) and "count" not in col_map:
                        col_map["count"] = ci
                    elif any(tr_upper(k) in cu for k in ["ARANAN", "NİTELİK", "AÇIKLAMA", "ŞART", "KOŞUL", "BAŞVURU"]) and "req" not in col_map:
                        col_map["req"] = ci
                break

        if header_idx is None or "title" not in col_map: continue

        last_faculty = ""
        for row in rows[header_idx + 1:]:
            cells = [clean(td.get_text()) for td in row.find_all(["th", "td"])]
            if not cells or not any(cells): continue

            def cell(key):
                idx = col_map.get(key)
                return cells[idx] if idx is not None and idx < len(cells) else ""

            faculty = cell("faculty")
            if faculty: last_faculty = faculty
            else: faculty = last_faculty

            dept      = cell("dept")
            title_raw = cell("title")
            count_raw = cell("count")
            req       = cell("req")

            digits = re.sub(r"\D", "", count_raw) or "1"
            count  = max(1, min(10, int(digits)))

            title_list = extract_titles_from_cell(title_raw)
            if not title_list: continue

            combined_title = " / ".join(title_list)
            primary_title  = title_list[0]

            ales = extract_ales(req, primary_title)
            if not ales["alesRequired"]: ales = extract_ales(full_text, primary_title)
            lang = extract_language(req, primary_title)
            if not lang["foreignLanguageRequired"]: lang = extract_language(full_text, primary_title)

            pos = {"faculty": faculty, "department": dept, "title": combined_title,
                   "count": count, "requirements": req, "_all_titles": title_list}
            pos.update(ales); pos.update(lang)
            positions.append(pos)

    return positions

def parse_positions_from_text(content_html: str, full_text: str) -> list:
    """
    Fallback parser for ads that use key-value text format instead of HTML tables.
    Handles patterns like:
        FAKÜLTE          : İktisadi, İdari ve Sosyal Bilimler Fakültesi
        BÖLÜM            : Tarih Bölümü
        AKADEMİK ÜNVANI  : Profesör
        ALINACAK AKADEMİSYEN SAYISI : 1
        ÖZEL KOŞULLAR    : ...
    Also handles repeated blocks for multiple positions.
    """
    soup = BeautifulSoup(content_html, "html.parser")
    text = soup.get_text("\n")
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    # Key synonyms for each field
    FACULTY_KEYS = ["FAKÜLTE", "BİRİM", "OKUL", "YUKSEKOKUL", "ENSTITU"]
    DEPT_KEYS    = ["BÖLÜM", "ANABİLİM", "PROGRAM", "ABD"]
    TITLE_KEYS   = ["ÜNVAN", "UNVAN", "KADRO", "AKADEMİK ÜNVAN", "AKADEMIK UNVAN"]
    COUNT_KEYS   = ["ADET", "SAYI", "ALINACAK", "KONTENJAN"]
    REQ_KEYS     = ["ÖZEL KOŞUL", "ARANAN NİTELİK", "AÇIKLAMA", "KOŞUL", "ŞART", "GENEL ŞART"]

    def match_key(line_up: str, keys: list) -> bool:
        return any(tr_upper(k) in line_up for k in keys)

    def extract_value(line: str) -> str:
        """Get the value after ':' in a key-value line."""
        if ":" in line:
            return line.split(":", 1)[1].strip()
        return ""

    # Try to find key-value structured content
    # First check if ANY title key exists in the text
    text_up = tr_upper(text)
    if not any(tr_upper(k) in text_up for k in TITLE_KEYS):
        return []

    positions = []

    # Strategy: scan for position blocks
    # Each block starts when we find a faculty or title key
    # and ends when we hit the next block or end of content
    i = 0
    current: dict = {}

    def flush(current: dict) -> None:
        if not current.get("title"):
            return
        title_list = extract_titles_from_cell(current["title"])
        if not title_list:
            return
        combined = " / ".join(title_list)
        primary = title_list[0]
        req = current.get("requirements", "")
        ales = extract_ales(req, primary)
        if not ales["alesRequired"]:
            ales = extract_ales(full_text, primary)
        lang = extract_language(req, primary)
        if not lang["foreignLanguageRequired"]:
            lang = extract_language(full_text, primary)
        count_str = re.sub(r"\D", "", current.get("count", "1")) or "1"
        count = max(1, min(10, int(count_str)))
        pos = {
            "faculty":     current.get("faculty", ""),
            "department":  current.get("dept", ""),
            "title":       combined,
            "count":       count,
            "requirements": req,
            "_all_titles": title_list,
        }
        pos.update(ales)
        pos.update(lang)
        positions.append(pos)

    # Pre-process: merge adjacent lines where key and value are split.
    # e.g. ["FAKÜLTE", ": İktisadi..."] → ["FAKÜLTE : İktisadi..."]
    merged_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # If next line starts with ":" and current line has no ":", merge them
        if (i + 1 < len(lines)
                and lines[i + 1].startswith(":")
                and ":" not in line):
            merged_lines.append(line + " " + lines[i + 1])
            i += 2
        else:
            merged_lines.append(line)
            i += 1
    lines = merged_lines

    i = 0
    while i < len(lines):
        line = lines[i]
        line_up = tr_upper(line)

        if match_key(line_up, FACULTY_KEYS) and ":" in line:
            # Starting a new block — flush previous
            if current:
                flush(current)
                current = {}
            current["faculty"] = extract_value(line)

        elif match_key(line_up, DEPT_KEYS) and ":" in line:
            current["dept"] = extract_value(line)

        elif match_key(line_up, TITLE_KEYS) and ":" in line:
            val = extract_value(line)
            if val:
                # If we already have a title, this is a new position
                if current.get("title") and val != current["title"]:
                    flush(current)
                    fac = current.get("faculty", "")
                    dept = current.get("dept", "")
                    current = {"faculty": fac, "dept": dept}
                current["title"] = val

        elif match_key(line_up, COUNT_KEYS) and ":" in line:
            val = extract_value(line)
            digits = re.sub(r"\D", "", val)
            if digits and int(digits) <= 10:
                current["count"] = digits

        elif match_key(line_up, REQ_KEYS) and ":" in line:
            # Collect requirement text — may span multiple lines
            req_parts = [extract_value(line)]
            j = i + 1
            while j < len(lines):
                next_up = tr_upper(lines[j])
                # Stop if we hit another key
                if any(match_key(next_up, keys) and ":" in lines[j]
                       for keys in [FACULTY_KEYS, DEPT_KEYS, TITLE_KEYS, COUNT_KEYS, REQ_KEYS]):
                    break
                req_parts.append(lines[j])
                j += 1
            current["requirements"] = " ".join(req_parts).strip()
            i = j - 1  # skip consumed lines

        i += 1

    # Flush last block
    if current:
        flush(current)

    return positions


# ── API calls ─────────────────────────────────────────────────────────────────────────────
def fetch_listing(skip_count: int) -> dict | None:
    body = {"keys": {"txv": [TAX_ID]}, "skipCount": skip_count, "maxResultCount": PAGE_SIZE}
    try:
        r = _session.post(LISTING_URL, json=body, timeout=TIMEOUT, verify=VERIFY_SSL)
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        log.error(f"Listing fetch failed (skip={skip_count}): {e}")
        return None

def fetch_detail(ad_id: str) -> dict | None:
    try:
        r = _session.get(DETAIL_URL, params={"id": ad_id}, timeout=TIMEOUT, verify=VERIFY_SSL)
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        log.error(f"Detail fetch failed (id={ad_id}): {e}")
        return None

def build_ad(item: dict, detail: dict, ulist: list) -> dict | None:
    content_html = detail.get("content", "") or ""
    full_text = BeautifulSoup(content_html, "html.parser").get_text()

    advertiser_name = item.get("advertiserName", "")
    api_city = item.get("addressCityName", "").title()
    matched_name, matched_city, uni_type = match_university(advertiser_name, ulist)
    city = api_city if api_city and api_city.upper() not in ("", "BİLİNMİYOR") else matched_city

    publish_date = None
    pd_str = item.get("publishStartDate", "")
    if pd_str:
        try: publish_date = datetime.fromisoformat(pd_str.replace("Z", "+00:00"))
        except ValueError: pass

    gazette_date_str = ""
    for f in item.get("adTypeFilters", []):
        if "Resmî Gazete" in f.get("key", "") or "Gazete" in f.get("key", ""):
            gazette_date_str = f.get("value", ""); break

    if not publish_date and gazette_date_str:
        m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", gazette_date_str)
        if m:
            try: publish_date = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=timezone.utc)
            except ValueError: pass

    if not publish_date: publish_date = datetime.now(timezone.utc)

    deadline  = extract_deadline(full_text, publish_date)

    # Primary: HTML table parser
    positions = parse_positions(content_html, full_text)

    # Fallback: key-value text parser (for ads like Bilkent that don't use tables)
    if not positions:
        positions = parse_positions_from_text(content_html, full_text)

    positions = [p for p in positions if is_academic(p.get("title", ""))]

    if not positions:
        log.info(f"  No academic positions — skipping.")
        return None

    detected_titles: list = []
    for p in positions:
        for t in p.pop("_all_titles", []):
            if t not in detected_titles and t in ACADEMIC_TITLES:
                detected_titles.append(t)

    snippet = clean(full_text)[:300]
    url = "https://www.ilan.gov.tr" + item.get("urlStr", "")

    return {
        "university":    matched_name,
        "city":          city,
        "uniType":       uni_type,
        "url":           url,
        "ilanNo":        item.get("adNo", ""),
        "publishDate":   publish_date.isoformat(),
        "deadline":      deadline,
        "detectedTitles": detected_titles,
        "positions":     positions,
        "contentSnippet": snippet,
        "applicationDocuments": [],
    }

# ── Main ───────────────────────────────────────────────────────────────────────────────────
def main():
    log.info("=== AkademikRadar Scraper (ilan.gov.tr API) Starting ===")
    log.info(f"Output: {OUTPUT_FILE}")

    existing: list = []
    existing_ids: set = set()
    existing_exam_calendar: list = []

    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                old = json.load(f)
            existing = old.get("ads", [])
            existing_ids = {ad.get("ilanNo", ad.get("url", "")) for ad in existing}
            existing_exam_calendar = old.get("examCalendar", [])
            log.info(f"Loaded {len(existing)} existing ads.")
        except Exception as e:
            log.warning(f"Could not load existing data: {e}")

    ulist = load_university_list()
    new_ads: list = []
    skip_count = 0
    stop = False

    while not stop and budget_ok():
        result = fetch_listing(skip_count)
        if not result: break

        total = result.get("numFound", 0)
        items = result.get("ads", [])
        log.info(f"Listing page skip={skip_count}: {len(items)} items (total={total})")
        if not items: break

        all_known = True
        for item in items:
            if not budget_ok():
                log.warning("Budget exhausted."); stop = True; break

            item_key = item.get("adNo") or item.get("urlStr", "")
            if item_key in existing_ids: continue

            all_known = False
            ad_id = item.get("id", "")
            log.info(f"  [{item.get('adNo','')}] {item.get('title','')[:60]}...")

            detail = fetch_detail(ad_id)
            if not detail: continue

            ad = build_ad(item, detail, ulist)
            if ad:
                new_ads.append(ad)
                existing_ids.add(item_key)
                log.info(f"    → {ad['university']} ({ad['city']}): {len(ad['positions'])} positions")

            time.sleep(REQUEST_DELAY)

        if all_known:
            log.info("All ads on this page already known — stopping."); break

        skip_count += PAGE_SIZE
        if skip_count >= total: break

    all_ads = new_ads + existing
    all_ads.sort(
        key=lambda a: datetime.fromisoformat(a.get("publishDate", "1970-01-01T00:00:00+00:00")),
        reverse=True
    )

    output = {
        "lastUpdated":  datetime.now(timezone.utc).isoformat(),
        "source":       "ilan.gov.tr",
        "ads":          all_ads,
        "examCalendar": existing_exam_calendar,
    }

    if os.path.dirname(OUTPUT_FILE):
        os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    log.info(f"=== Done in {int(time.time()-_start_time)}s. "
             f"{len(new_ads)} new + {len(existing)} kept = {len(all_ads)} total ===")

if __name__ == "__main__":
    main()
