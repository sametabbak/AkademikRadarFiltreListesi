# -*- coding: utf-8 -*-
"""
AkademikRadar Scraper
Scrapes academic job announcements from Resmî Gazete and produces ilanlar.json.
"""

import requests
from bs4 import BeautifulSoup
import json, re, io, os, time, logging
from datetime import datetime, timezone, timedelta

try:
    import pdfplumber
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Runtime limits ────────────────────────────────────────────────────────────
# Hard wall-clock budget. Scraper writes whatever it has and exits cleanly
# before the GitHub Actions job timeout can kill it.
MAX_RUNTIME_SECONDS = 20 * 60        # 20 minutes total
MAX_PDFS_PER_RUN    = 30             # cap to avoid runaway on busy gazette days

# ── Request timeouts ──────────────────────────────────────────────────────────
# Use (connect, read) tuple:  connect must succeed in 10 s, each read chunk
# in 25 s.  This is different from a *total* download timeout, but it prevents
# the most common hang: server accepts the connection then sends data very slowly.
CONNECT_TIMEOUT = 10
READ_TIMEOUT    = 25
TIMEOUT         = (CONNECT_TIMEOUT, READ_TIMEOUT)
MAX_PDF_BYTES   = 6 * 1024 * 1024   # skip PDFs larger than 6 MB (corrupt / huge)

OUTPUT_FILE = "ilanlar.json"
DAYS_TO_CHECK = 5
RG_BASE = "https://www.resmigazete.gov.tr"
UNIVERSITY_LIST_URL = (
    "https://raw.githubusercontent.com/sametabbak/AkademikRadarFiltreListesi"
    "/refs/heads/main/TurkishUniversityList"
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/pdf,*/*",
    "Accept-Language": "tr-TR,tr;q=0.9",
}

ACADEMIC_TITLES = {
    "PROFESÖR",
    "DOÇENT",
    "DR. ÖĞR. ÜYESİ",
    "ÖĞRETİM GÖREVLİSİ",
    "ARAŞTIRMA GÖREVLİSİ",
}

TITLE_ALIASES = {
    "PROF.":                 "PROFESÖR",
    "PROF. DR.":             "PROFESÖR",
    "DOÇ.":                  "DOÇENT",
    "DOÇ. DR.":              "DOÇENT",
    "YARDIMCI DOÇENT":       "DR. ÖĞR. ÜYESİ",
    "DR. ÖĞR.":              "DR. ÖĞR. ÜYESİ",
    "DR.ÖĞR.ÜYESİ":          "DR. ÖĞR. ÜYESİ",
    "DOKTOR ÖĞRETİM ÜYESİ": "DR. ÖĞR. ÜYESİ",
    "ÖĞR. GÖR.":             "ÖĞRETİM GÖREVLİSİ",
    "ÖĞRETİM GÖR.":          "ÖĞRETİM GÖREVLİSİ",
    "ARŞ. GÖR.":             "ARAŞTIRMA GÖREVLİSİ",
    "ARAŞTIRMA GÖR.":        "ARAŞTIRMA GÖREVLİSİ",
}

TR_MONTHS = {
    "Ocak":1,"Şubat":2,"Mart":3,"Nisan":4,"Mayıs":5,"Haziran":6,
    "Temmuz":7,"Ağustos":8,"Eylül":9,"Ekim":10,"Kasım":11,"Aralık":12,
}

# ── Global time budget ────────────────────────────────────────────────────────
_start_time = time.monotonic()

def time_remaining() -> float:
    return MAX_RUNTIME_SECONDS - (time.monotonic() - _start_time)

def budget_ok() -> bool:
    remaining = time_remaining()
    if remaining <= 0:
        log.warning(f"Time budget exhausted after {MAX_RUNTIME_SECONDS}s — writing output now.")
    return remaining > 0

# ── Turkish string helpers ────────────────────────────────────────────────────

def tr_upper(s: str) -> str:
    return s.replace("i", "İ").replace("ı", "I").upper()

def normalize_for_match(s: str) -> str:
    """Flatten all Turkish i-variants + accented chars to plain ASCII for matching."""
    return (
        s
        .replace("İ", "I").replace("ı", "I").replace("i", "I")
        .replace("ğ", "g").replace("Ğ", "G")
        .replace("ş", "s").replace("Ş", "S")
        .replace("ç", "c").replace("Ç", "C")
        .replace("ö", "o").replace("Ö", "O")
        .replace("ü", "u").replace("Ü", "U")
        .upper()
    )

def clean_cell(value: str) -> str:
    if not value: return ""
    text = re.sub(r"[\r\n]+", " ", value)
    return re.sub(r"[ \t]{2,}", " ", text).strip()

# ── HTTP session ──────────────────────────────────────────────────────────────
# One session, no automatic retries (we handle retries ourselves with time checks).
_session = requests.Session()
_session.headers.update(HEADERS)
# Disable urllib3 automatic retries — we want full control
from requests.adapters import HTTPAdapter
_adapter = HTTPAdapter(max_retries=0)
_session.mount("http://", _adapter)
_session.mount("https://", _adapter)

def fetch_html(url: str) -> BeautifulSoup | None:
    if not budget_ok(): return None
    try:
        r = _session.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log.warning(f"HTML fetch failed [{url}]: {e}")
        return None

def fetch_bytes(url: str, retries: int = 2) -> bytes | None:
    """
    Download a binary file with a hard per-attempt timeout.
    Uses streaming + manual accumulation so we can enforce a MAX_PDF_BYTES cap
    and abort oversized or stalled downloads early.
    """
    if not budget_ok(): return None
    for attempt in range(1, retries + 1):
        if not budget_ok(): return None
        try:
            with _session.get(url, timeout=TIMEOUT, stream=True) as r:
                r.raise_for_status()

                # Check Content-Length header upfront
                content_length = int(r.headers.get("Content-Length", 0))
                if content_length > MAX_PDF_BYTES:
                    log.warning(f"  PDF too large ({content_length} bytes), skipping: {url}")
                    return None

                chunks = []
                total = 0
                chunk_deadline = time.monotonic() + READ_TIMEOUT
                for chunk in r.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_PDF_BYTES:
                        log.warning(f"  PDF exceeded {MAX_PDF_BYTES} bytes mid-download, skipping.")
                        return None
                    if time.monotonic() > chunk_deadline:
                        log.warning(f"  PDF download stalled (no new chunk in {READ_TIMEOUT}s), skipping.")
                        return None
                    chunk_deadline = time.monotonic() + READ_TIMEOUT  # reset on each chunk
                    chunks.append(chunk)

                return b"".join(chunks)

        except Exception as e:
            log.warning(f"  Bytes fetch attempt {attempt} failed [{url}]: {e}")
            if attempt < retries:
                time.sleep(3)
    return None

# ── URL helpers ───────────────────────────────────────────────────────────────

def build_index_url(date: datetime) -> str:
    return (
        f"{RG_BASE}/ilanlar/eskiilanlar/"
        f"{date.strftime('%Y')}/{date.strftime('%m')}/"
        f"{date.strftime('%Y%m%d')}-4.htm"
    )

def resolve_url(href: str, index_url: str) -> str:
    if href.startswith("http"): return href
    if href.startswith("/"): return RG_BASE + href
    return index_url.rsplit("/", 1)[0] + "/" + href

def to_pdf_url(url: str) -> str:
    if url.endswith(".htm"): return url[:-4] + ".pdf"
    if url.endswith(".pdf"): return url
    return url + ".pdf"

# ── University list ───────────────────────────────────────────────────────────

# Local cache path for university list — survives across runs
UNIVERSITY_CACHE_FILE = "university_list_cache.json"

# Hardcoded fallback — covers universities most likely to appear in Resmî Gazete
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
  { "Name": "ZONGULDAK BÜLENT ECEVİT ÜNİVERSİTESİ", "City": "Zonguldak", "Type": "Devlet" }
  { "Name": "ABDULLAH GÜL ÜNİVERSİTESİ", "City": "Kayseri", "Type": "Devlet" },
  { "Name": "ALANYA ÜNİVERSİTESİ", "City": "Antalya", "Type": "Vakıf" },
  { "Name": "ANKARA MÜZİK VE GÜZEL SANATLAR ÜNİVERSİTESİ", "City": "Ankara", "Type": "Devlet" },
  { "Name": "ANTALYA BELEK ÜNİVERSİTESİ", "City": "Antalya", "Type": "Vakıf" },
  { "Name": "TOROS ÜNİVERSİTESİ", "City": "Mersin", "Type": "Vakıf" }
]

def load_university_list() -> list:
    """
    Load university list with 3 layers of fallback:
    1. Fetch from GitHub URL (with 3 retries)
    2. Read from local cache file (written on last successful fetch)
    3. Use hardcoded fallback list
    """
    # Layer 1: Try fetching from URL with retries
    for attempt in range(1, 4):
        try:
            r = _session.get(UNIVERSITY_LIST_URL, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if data:
                log.info(f"Loaded {len(data)} universities from URL.")
                # Save to local cache for future fallback
                try:
                    with open(UNIVERSITY_CACHE_FILE, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False)
                except Exception:
                    pass
                return data
        except Exception as e:
            log.warning(f"University list fetch attempt {attempt} failed: {e}")
            if attempt < 3:
                time.sleep(3)

    # Layer 2: Try local cache
    if os.path.exists(UNIVERSITY_CACHE_FILE):
        try:
            with open(UNIVERSITY_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data:
                log.info(f"Loaded {len(data)} universities from local cache.")
                return data
        except Exception as e:
            log.warning(f"Local cache read failed: {e}")

    # Layer 3: Hardcoded fallback
    log.warning(f"Using hardcoded fallback list ({len(FALLBACK_UNIVERSITY_LIST)} universities).")
    return FALLBACK_UNIVERSITY_LIST

def match_university(name: str, ulist: list) -> tuple:
    """
    Match a raw name string (from PDF or link text) against the university list.
    Returns the canonical name, city and type from the list if found.

    Two-pass matching:
    Pass 1 — with spaces preserved: handles clean text that already has spaces.
    Pass 2 — spaces stripped from both sides: handles run-together PDF text like
              'KARAMANOGLUMEHMETBEYUNIVERSITESI' which should match
              'KARAMANOGLU MEHMETBEY UNIVERSITESI' from the list.
    The canonical name from the list is ALWAYS what gets stored — never the raw PDF text.
    """
    name_norm       = normalize_for_match(clean_cell(name))
    name_norm_nsp   = name_norm.replace(" ", "").replace("-", "")   # space+hyphen stripped

    best, best_len = None, 0

    for uni in ulist:
        u_norm     = normalize_for_match(uni["Name"])
        u_norm_nsp = u_norm.replace(" ", "").replace("-", "")

        # Pass 1: substring match with spaces intact
        matched = u_norm in name_norm or name_norm in u_norm
        # Pass 2: substring match ignoring spaces (catches run-together words)
        if not matched:
            matched = u_norm_nsp in name_norm_nsp or name_norm_nsp in u_norm_nsp

        if matched and len(u_norm) > best_len:
            best, best_len = uni, len(u_norm)

    if best:
        # Always write the clean canonical name from our list, never the raw PDF text
        return best["Name"], best["City"], best["Type"]

    log.warning(f"  No university match found for: '{name.strip()}'")
    return tr_upper(name.strip()), "Bilinmiyor", "Devlet"

# ── Existing JSON (deduplication) ─────────────────────────────────────────────

def load_existing_ads() -> tuple[list, set]:
    if not os.path.exists(OUTPUT_FILE):
        return [], set()
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        ads = data.get("ads", [])
        urls = {ad["url"] for ad in ads if "url" in ad}
        log.info(f"Loaded {len(ads)} existing ads ({len(urls)} URLs).")
        return ads, urls
    except Exception as e:
        log.warning(f"Could not read {OUTPUT_FILE}: {e}")
        return [], set()

# ── PDF text cleaning ─────────────────────────────────────────────────────────

def clean_pdf_text(raw: str) -> str:
    text = re.sub(r"-\n", "", raw)
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    text = re.sub(r"([a-zçğışöüA-ZÇĞİÖŞÜ])([A-ZÇĞİÖŞÜ]{2,})", r"\1 \2", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    lines = [l.strip() for l in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

# ── Title helpers ─────────────────────────────────────────────────────────────

def normalize_title(raw: str) -> str:
    raw_up = tr_upper(raw.strip())
    for alias, canonical in TITLE_ALIASES.items():
        if tr_upper(alias) in raw_up:
            return canonical
    for t in ACADEMIC_TITLES:
        if tr_upper(t) in raw_up:
            return t
    return raw_up

def is_academic(title: str) -> bool:
    return title in ACADEMIC_TITLES

# ── Extraction helpers ────────────────────────────────────────────────────────

def extract_university_from_link_text(link_text: str) -> str:
    """
    Extract the university name portion from a gazette link text.
    e.g. "Hacettepe Üniversitesi Rektörlüğünden" → "HACETTEPE ÜNİVERSİTESİ"
         "KARAMANOĞLUMEHMETBEYÜNİVERSİTESİ Rektörlüğünden" → "KARAMANOĞLUMEHMETBEYÜNIVERSITESI"
    The raw result (possibly run-together) is then passed to match_university
    which handles space-stripped comparison.
    """
    cleaned = clean_cell(link_text)
    up = tr_upper(cleaned)
    for marker in [tr_upper("REKTÖRLÜĞÜNDEN"), tr_upper("REKTORLUGUNDEN"), "REKTORLUGUNDEN"]:
        if marker in up:
            return up.split(marker)[0].strip()
    return up

def extract_university_from_text(text: str, ulist: list) -> str:
    """
    Scan the full PDF text for any university name from the list.
    Uses space-stripped comparison so run-together words still match.
    Returns the canonical name from the list if found.
    """
    text_norm     = normalize_for_match(text)
    text_norm_nsp = text_norm.replace(" ", "")
    best, best_len = None, 0

    for uni in ulist:
        u_norm     = normalize_for_match(uni["Name"])
        u_norm_nsp = u_norm.replace(" ", "").replace("-", "")
        # Try both with-spaces and without-spaces
        if (u_norm in text_norm or u_norm_nsp in text_norm_nsp) and len(u_norm) > best_len:
            best, best_len = uni["Name"], len(u_norm)

    if best:
        return best
    return "Bilinmiyor"

def extract_deadline(text: str, publish_date: datetime) -> str | None:
    m = re.search(
        r"son\s+başvuru\s+tarih\w*[:\s]*(\d{1,2})[./](\d{2})[./](\d{4})",
        text, re.IGNORECASE
    )
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        tm = re.search(
            r"saat\s*(\d{1,2})[:\.](\d{2})",
            text[max(0, m.start()-20): m.start()+200], re.IGNORECASE
        )
        h, mi = (int(tm.group(1)), int(tm.group(2))) if tm else (23, 59)
        try: return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).isoformat()
        except ValueError: pass

    for mname, mnum in TR_MONTHS.items():
        for m in re.finditer(rf"(\d{{1,2}})\s+{mname}\s+(\d{{4}})", text, re.IGNORECASE):
            d, y = int(m.group(1)), int(m.group(2))
            try:
                dt = datetime(y, mnum, d, 23, 59, tzinfo=timezone.utc)
                if dt > publish_date: return dt.isoformat()
            except ValueError: pass

    m = re.search(
        r"(?:ilan[ıi]n?\s+yay[ıi]m\w*\s+tarihi[nk]den\s+itibaren|ilan\s+tarihinden\s+itibaren)[^0-9]*(\d+)",
        text, re.IGNORECASE
    )
    if m:
        days = int(m.group(1))
        if 7 <= days <= 60:
            return (publish_date + timedelta(days=days)).replace(tzinfo=timezone.utc).isoformat()
    return None

# Titles that are legally exempt from ALES requirement
ALES_EXEMPT_TITLES = {"PROFESÖR", "DOÇENT"}

def extract_ales(text: str, title: str = "") -> dict:
    r = {"alesRequired": False, "alesScore": None, "alesType": None}
    # Profesör and Doçent are legally exempt from ALES
    if title in ALES_EXEMPT_TITLES:
        return r
    if "ALES" not in tr_upper(text): return r
    r["alesRequired"] = True
    m = re.search(r"ALES[^0-9\n]{0,60}?(\d{2,3})\s*(?:ve üzeri|veya üzeri|puan|puanı)", text, re.IGNORECASE)
    if not m: m = re.search(r"en\s+az\s*(\d{2,3})\s*(?:ALES|puan)", text, re.IGNORECASE)
    if m: r["alesScore"] = int(m.group(1))
    tm = re.search(r"ALES[^()\n]{0,60}?\b(SAY|SÖZ|EA|DİL|DIL)\b", text, re.IGNORECASE)
    if tm: r["alesType"] = tm.group(1).upper().replace("DIL", "DİL")
    return r

def extract_language(text: str, title: str = "") -> dict:
    r = {"foreignLanguageRequired": False, "foreignLanguageScore": None, "foreignLanguageExam": None}
    for exam in ["YÖKDİL", "YOKDIL", "E-YDS", "YDS", "TOEFL", "IELTS"]:
        if exam in tr_upper(text):
            r["foreignLanguageRequired"] = True
            r["foreignLanguageExam"] = exam.replace("YOKDIL", "YÖKDİL")
            m = re.search(rf"{re.escape(exam)}[^0-9\n]{{0,60}}?(\d{{2,3}})\s*(?:ve üzeri|puan|puanı)", text, re.IGNORECASE)
            if m: r["foreignLanguageScore"] = int(m.group(1))
            break
    if not r["foreignLanguageRequired"] and re.search(r"yabancı\s+dil", text, re.IGNORECASE):
        r["foreignLanguageRequired"] = True
    return r

def extract_documents(text: str) -> list:
    docs, tl = [], text.lower()
    for kw, label in [
        ("özgeçmiş","Özgeçmiş"), ("nüfus cüzdan","Nüfus Cüzdanı Sureti"),
        ("diploma","Diploma Fotokopisi"), ("ales belgesi","ALES Belgesi"),
        ("ales sonuç","ALES Sonuç Belgesi"), ("yds belgesi","YDS Belgesi"),
        ("yokdil belgesi","YÖKDİL Belgesi"), ("yabancı dil belgesi","Yabancı Dil Belgesi"),
        ("fotoğraf","Vesikalık Fotoğraf"), ("askerlik","Askerlik Durum Belgesi"),
        ("transkript","Transkript (Not Döküm Belgesi)"), ("not döküm","Transkript (Not Döküm Belgesi)"),
        ("yayın listesi","Yayın Listesi"), ("sabıka","Sabıka Kaydı"),
        ("doktora belgesi","Doktora Belgesi"), ("doçentlik belgesi","Doçentlik Belgesi"),
        ("başvuru dilekçe","Başvuru Dilekçesi"), ("öğrenci belgesi","Öğrenci Belgesi"),
    ]:
        if kw in tl and label not in docs: docs.append(label)
    return docs

# ── Table / text position extractors ─────────────────────────────────────────

FACULTY_KEYS = ["FAKÜLTESİ","YÜKSEKOKUL","ENSTİTÜSÜ","MYO","MESLEK","BİRİM","OKUL"]
DEPT_KEYS    = ["ANABİLİM","PROGRAM","BÖLÜM","DAL","ALAN"]
TITLE_KEYS   = ["UNVAN","ÜNVAN","KADRO ÜNVANI","POZİSYON","ÜNVANI"]
COUNT_KEYS   = ["SAYI","ADET","KADRO ADEDİ","KADRO SAYISI"]
REQ_KEYS     = ["AÇIKLAMA","NİTELİK","ÖZEL ŞART","ARANAN ŞART","KOŞUL","NİTELİKLER"]

def extract_positions_from_tables(tables: list, full_text: str) -> list:
    positions = []
    for table in tables:
        if not table or len(table) < 2: continue
        header_idx = None
        for i, row in enumerate(table[:5]):
            row_up = tr_upper(" ".join(str(c or "") for c in row))
            hits = sum(1 for k in TITLE_KEYS + COUNT_KEYS + FACULTY_KEYS if tr_upper(k) in row_up)
            if hits >= 2: header_idx = i; break
        if header_idx is None: continue
        header = table[header_idx]
        col: dict[str, int] = {}
        for j, cell in enumerate(header):
            cu = tr_upper(str(cell or "").strip())
            if "faculty"  not in col and any(tr_upper(k) in cu for k in FACULTY_KEYS): col["faculty"] = j
            elif "dept"   not in col and any(tr_upper(k) in cu for k in DEPT_KEYS):    col["dept"] = j
            elif "title"  not in col and any(tr_upper(k) in cu for k in TITLE_KEYS):   col["title"] = j
            elif "count"  not in col and any(tr_upper(k) in cu for k in COUNT_KEYS):   col["count"] = j
            elif "req"    not in col and any(tr_upper(k) in cu for k in REQ_KEYS):     col["req"] = j
        if not col: continue
        last_faculty = ""
        for row in table[header_idx+1:]:
            if not row or not any(row): continue
            row = [clean_cell(str(c or "")) for c in row]
            pos: dict = {}
            if "faculty" in col:
                v = row[col["faculty"]]
                if v: last_faculty = v
                pos["faculty"] = last_faculty
            else:
                pos["faculty"] = ""
            pos["department"]   = row[col["dept"]]  if "dept"  in col else ""
            pos["requirements"] = row[col["req"]]   if "req"   in col else ""
            pos["count"]        = max(1, int(re.sub(r"\D","", row[col["count"]] or "1") or "1")) if "count" in col else 1
            pos["title"]        = normalize_title(row[col["title"]]) if "title" in col else \
                                  next((t for t in ACADEMIC_TITLES if tr_upper(t) in tr_upper(" ".join(row))), "")
            if not pos["title"] and not pos["faculty"] and not pos["department"]: continue
            ctx = pos["requirements"] + "\n" + full_text
            pos.update(extract_ales(ctx, pos.get("title", ""))); pos.update(extract_language(ctx, pos.get("title", "")))
            positions.append(pos)
    return positions

def extract_positions_from_text(full_text: str) -> list:
    positions, lines = [], [l.strip() for l in full_text.split("\n") if l.strip()]
    current_faculty = ""
    for i, line in enumerate(lines):
        lu = tr_upper(line)
        if any(tr_upper(k) in lu for k in ["FAKÜLTESİ","YÜKSEKOKULU","ENSTİTÜSÜ","MYO"]):
            if not any(tr_upper(t) in lu for t in ACADEMIC_TITLES):
                current_faculty = line.strip(); continue
        found = next((t for t in ACADEMIC_TITLES if tr_upper(t) in lu), None)
        if not found: continue
        cnt = int(m.group(1)) if (m := re.search(r'\b(\d{1,2})\b', line)) else 1
        dept = lu.split(tr_upper(found))[0].strip()
        reqs = []
        for j in range(i+1, min(i+6, len(lines))):
            nu = tr_upper(lines[j])
            if any(tr_upper(t) in nu for t in ACADEMIC_TITLES): break
            if any(tr_upper(k) in nu for k in ["FAKÜLTESİ","YÜKSEKOKULU"]): break
            if lines[j].strip(): reqs.append(lines[j].strip())
        req = " ".join(reqs)
        pos = {"faculty": current_faculty, "department": dept.title() if dept else "",
               "title": found, "count": cnt, "requirements": req}
        pos.update(extract_ales(req+"\n"+full_text, found)); pos.update(extract_language(req+"\n"+full_text, found))
        positions.append(pos)
    return positions

def generate_snippet(university: str, positions: list, deadline: str | None) -> str:
    if not positions: return f"{university} akademik personel alım ilanı."
    tc: dict[str, int] = {}
    for p in positions:
        t = p.get("title","")
        if is_academic(t): tc[t] = tc.get(t,0) + p.get("count",1)
    summary = ", ".join(f"{c} {t}" for t, c in tc.items())
    faculties = list(dict.fromkeys(p.get("faculty","") for p in positions if p.get("faculty")))
    fac_str = ", ".join(faculties[:3]) + (f" ve {len(faculties)-3} birim daha" if len(faculties) > 3 else "")
    snippet = f"{university} bünyesine {summary} alınacaktır."
    if fac_str: snippet += f" Birimler: {fac_str}."
    if deadline:
        try: snippet += f" Son başvuru: {datetime.fromisoformat(deadline).strftime('%d.%m.%Y')}."
        except: pass
    return snippet

# ── PDF parser ────────────────────────────────────────────────────────────────

def parse_pdf(pdf_bytes: bytes, link_text: str, publish_date: datetime, ulist: list) -> dict | None:
    if not PDF_AVAILABLE: return None
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            raw_pages = [page.extract_text() or "" for page in pdf.pages]
            all_tables: list = []
            for page in pdf.pages:
                try:
                    tbls = page.extract_tables()
                    if tbls: all_tables.extend(tbls)
                except: pass
    except Exception as e:
        log.warning(f"pdfplumber failed: {e}"); return None

    full_text = "\n\n".join(clean_pdf_text(p) for p in raw_pages if p.strip())
    if not full_text.strip(): return None

    raw_from_link = extract_university_from_link_text(link_text)
    uni_name, city, uni_type = match_university(raw_from_link, ulist)
    if city == "Bilinmiyor":
        n2, c2, t2 = match_university(extract_university_from_text(full_text, ulist), ulist)
        if c2 != "Bilinmiyor": uni_name, city, uni_type = n2, c2, t2

    deadline  = extract_deadline(full_text, publish_date)
    positions = extract_positions_from_tables(all_tables, full_text)
    if not positions: positions = extract_positions_from_text(full_text)
    positions = [p for p in positions if is_academic(p.get("title",""))]
    if not positions:
        log.info(f"  Skipping {uni_name} — no academic titles."); return None

    docs     = extract_documents(full_text)
    snippet  = generate_snippet(uni_name, positions, deadline)
    detected = list(dict.fromkeys(p["title"] for p in positions if is_academic(p["title"])))

    return {
        "university": uni_name, "city": city, "uniType": uni_type,
        "publishDate": publish_date.isoformat(), "deadline": deadline,
        "detectedTitles": detected, "contentSnippet": snippet,
        "positions": positions, "applicationDocuments": docs,
    }

# ── Exam calendar ─────────────────────────────────────────────────────────────

EXAM_META = {
    "ALES":    {"field":"Kariyer","url":"https://www.osym.gov.tr"},
    "YDS":     {"field":"Dil",    "url":"https://www.osym.gov.tr"},
    "E-YDS":   {"field":"Dil",    "url":"https://www.osym.gov.tr"},
    "YÖK-DİL": {"field":"Dil",    "url":"https://yokdil.yok.gov.tr"},
    "YOKDIL":  {"field":"Dil",    "url":"https://yokdil.yok.gov.tr"},
    "TUS":     {"field":"Tıp",    "url":"https://www.osym.gov.tr"},
    "DUS":     {"field":"Diş",    "url":"https://www.osym.gov.tr"},
    "E-TEP":   {"field":"Dil",    "url":"https://www.osym.gov.tr"},
    "STS":     {"field":"Tıp",    "url":"https://www.osym.gov.tr"},
}

def fetch_exam_calendar() -> list:
    if not budget_ok(): return []
    exams: list = []
    for url in ["https://www.osym.gov.tr/TR,6/sinav-takvimi.html", "https://www.osym.gov.tr/TR,6/"]:
        soup = fetch_html(url)
        if not soup: continue
        for row in soup.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in row.find_all(["td","th"])]
            if len(cells) < 2: continue
            row_text = " ".join(cells)
            matched_name, meta = None, None
            for name, m in EXAM_META.items():
                if name in tr_upper(row_text):
                    matched_name, meta = name, m; break
            if not matched_name: continue
            dm = re.search(r"(\d{1,2})[.\-/](\d{2})[.\-/](\d{4})", row_text)
            if dm:
                try:
                    dt = datetime(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))
                    exams.append({"shortName": f"{matched_name} {dt.year}", "name": cells[0],
                                  "field": meta["field"], "examDate": dt.strftime("%Y-%m-%d"),
                                  "year": dt.year, "officialUrl": meta["url"]})
                    continue
                except ValueError: pass
            for mname, mnum in TR_MONTHS.items():
                m2 = re.search(rf"(\d{{1,2}})\s+{mname}\s+(\d{{4}})", row_text, re.IGNORECASE)
                if m2:
                    try:
                        dt = datetime(int(m2.group(2)), mnum, int(m2.group(1)))
                        exams.append({"shortName": f"{matched_name} {dt.year}", "name": cells[0],
                                      "field": meta["field"], "examDate": dt.strftime("%Y-%m-%d"),
                                      "year": dt.year, "officialUrl": meta["url"]})
                    except ValueError: pass
                    break
        if exams: break
    log.info(f"Exam calendar: {len(exams)} entries.")
    return exams

# ── Day scraper ───────────────────────────────────────────────────────────────

def scrape_day(date: datetime, ulist: list, existing_urls: set,
               pdf_counter: list) -> list:
    """
    pdf_counter is a mutable 1-element list used to share the count
    across calls without globals.
    """
    if not budget_ok(): return []
    index_url = build_index_url(date)
    log.info(f"Checking {date.strftime('%Y-%m-%d')}: {index_url}")
    soup = fetch_html(index_url)
    if not soup:
        log.info("  No gazette page."); return []

    links: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        lt = a.get_text(strip=True)
        if "Rektörlüğünden" in lt:
            abs_url = resolve_url(a["href"], index_url)
            pdf_url = to_pdf_url(abs_url)
            if pdf_url in existing_urls:
                log.info(f"  Already known: {pdf_url}"); continue
            links.append((pdf_url, lt))

    log.info(f"  {len(links)} new Rektörlüğünden links.")
    ads: list = []

    for pdf_url, lt in links:
        if not budget_ok(): break
        if pdf_counter[0] >= MAX_PDFS_PER_RUN:
            log.warning(f"  Reached MAX_PDFS_PER_RUN={MAX_PDFS_PER_RUN}, stopping."); break

        pdf_counter[0] += 1
        log.info(f"  [{pdf_counter[0]}/{MAX_PDFS_PER_RUN}] Downloading: {pdf_url}")
        time.sleep(0.5)

        pdf_bytes = fetch_bytes(pdf_url)
        if not pdf_bytes:
            log.warning(f"  Download failed: {pdf_url}"); continue

        parsed = parse_pdf(pdf_bytes, lt, date, ulist)
        if parsed is None: continue
        parsed["url"] = pdf_url
        ads.append(parsed)
        log.info(f"  ✓ {parsed['university']} — {parsed['detectedTitles']}")

    return ads

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=== AkademikRadar Scraper Starting ===")
    log.info(f"Budget: {MAX_RUNTIME_SECONDS}s | Max PDFs: {MAX_PDFS_PER_RUN}")

    if not PDF_AVAILABLE:
        log.error("pdfplumber not installed. Run: pip install pdfplumber")
        raise SystemExit(1)

    ulist = load_university_list()
    existing_ads, existing_urls = load_existing_ads()

    pdf_counter = [0]  # mutable counter shared across scrape_day calls
    new_ads: list = []
    today = datetime.now(timezone.utc)

    for i in range(DAYS_TO_CHECK):
        if not budget_ok(): break
        new_ads.extend(scrape_day(today - timedelta(days=i), ulist, existing_urls, pdf_counter))

    # Deduplicate new ads
    seen: set = set()
    unique_new: list = []
    for ad in new_ads:
        if ad["url"] not in seen:
            seen.add(ad["url"]); unique_new.append(ad)

    # Merge, prune > 90 days, sort
    cutoff = today - timedelta(days=90)
    all_ads = unique_new + existing_ads
    all_ads = [
        ad for ad in all_ads
        if datetime.fromisoformat(
            ad.get("publishDate", today.isoformat())
        ).replace(tzinfo=timezone.utc) >= cutoff
    ]
    all_ads.sort(key=lambda x: x.get("publishDate",""), reverse=True)

    exam_calendar = fetch_exam_calendar()

    output = {
        "generatedAt": today.isoformat(),
        "count": len(all_ads),
        "ads": all_ads,
        "examCalendar": exam_calendar,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    elapsed = int(time.monotonic() - _start_time)
    log.info(
        f"=== Done in {elapsed}s. "
        f"{len(unique_new)} new + {len(existing_ads)} kept = {len(all_ads)} total ==="
    )

if __name__ == "__main__":
    main()
