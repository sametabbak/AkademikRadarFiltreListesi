# -*- coding: utf-8 -*-
"""
AkademikRadar Scraper
Scrapes academic job announcements from Resmî Gazete and produces ilanlar.json.
(Hibrit Model: pdfplumber + Gemini API Yedek Planı)
"""

import requests
from bs4 import BeautifulSoup
import json, re, io, os, time, logging
from datetime import datetime, timezone, timedelta

# Gemini API entegrasyonu
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

try:
    import pdfplumber
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Gemini API Yapılandırması ─────────────────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if GEMINI_AVAILABLE and GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
elif not GEMINI_API_KEY:
    log.warning("GEMINI_API_KEY bulunamadı. Yapay zeka destekli yedek plan çalışmayacak.")

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
    "PROF.":                      "PROFESÖR",
    "PROF. DR.":                  "PROFESÖR",
    "DOÇ.":                       "DOÇENT",
    "DOÇ. DR.":                   "DOÇENT",
    "DOÇENT DR.":                 "DOÇENT",
    "YARDIMCI DOÇENT":            "DR. ÖĞR. ÜYESİ",
    "DR. ÖĞR.":                   "DR. ÖĞR. ÜYESİ",
    "DR.ÖĞR.ÜYESİ":               "DR. ÖĞR. ÜYESİ",
    "DOKTOR ÖĞRETİM ÜYESİ":      "DR. ÖĞR. ÜYESİ",
    "DR. ÖĞRETİM ÜYESİ":         "DR. ÖĞR. ÜYESİ",
    "DR.ÖĞRETİM ÜYESİ":          "DR. ÖĞR. ÜYESİ",
    "DR ÖĞRETİM ÜYESİ":          "DR. ÖĞR. ÜYESİ",
    "ÖĞR. GÖR.":                  "ÖĞRETİM GÖREVLİSİ",
    "ÖĞRETİM GÖR.":               "ÖĞRETİM GÖREVLİSİ",
    "ARŞ. GÖR.":                  "ARAŞTIRMA GÖREVLİSİ",
    "ARAŞTIRMA GÖR.":             "ARAŞTIRMA GÖREVLİSİ",
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
_session = requests.Session()
_session.headers.update(HEADERS)
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
    if not budget_ok(): return None
    for attempt in range(1, retries + 1):
        if not budget_ok(): return None
        try:
            with _session.get(url, timeout=TIMEOUT, stream=True) as r:
                r.raise_for_status()

                content_length = int(r.headers.get("Content-Length", 0))
                if content_length > MAX_PDF_BYTES:
                    log.warning(f"  PDF too large ({content_length} bytes), skipping: {url}")
                    return None

                chunks = []
                total = 0
                chunk_deadline = time.monotonic() + READ_TIMEOUT
                for chunk in r.iter_content(chunk_size=65536):
                    if not chunk: continue
                    total += len(chunk)
                    if total > MAX_PDF_BYTES:
                        log.warning(f"  PDF exceeded {MAX_PDF_BYTES} bytes mid-download, skipping.")
                        return None
                    if time.monotonic() > chunk_deadline:
                        log.warning(f"  PDF download stalled, skipping.")
                        return None
                    chunk_deadline = time.monotonic() + READ_TIMEOUT 
                    chunks.append(chunk)

                return b"".join(chunks)

        except Exception as e:
            log.warning(f"  Bytes fetch attempt {attempt} failed [{url}]: {e}")
            if attempt < retries: time.sleep(3)
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

UNIVERSITY_CACHE_FILE = "university_list_cache.json"

FALLBACK_UNIVERSITY_LIST = [
    {"Name": "ANKARA ÜNİVERSİTESİ", "City": "Ankara", "Type": "Devlet"},
    {"Name": "GAZİ ÜNİVERSİTESİ", "City": "Ankara", "Type": "Devlet"},
    {"Name": "HACETTEPE ÜNİVERSİTESİ", "City": "Ankara", "Type": "Devlet"},
    {"Name": "İSTANBUL ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Devlet"},
    {"Name": "MARMARA ÜNİVERSİTESİ", "City": "İstanbul", "Type": "Devlet"},
    {"Name": "EGE ÜNİVERSİTESİ", "City": "İzmir", "Type": "Devlet"},
    {"Name": "DOKUZ EYLÜL ÜNİVERSİTESİ", "City": "İzmir", "Type": "Devlet"},
    # Fallback listesi çok uzun olduğu için orijinal dosyadaki tamamı kopyalanabilir,
    # burada örnek kısa tutulmuştur, asıl projenizdeki listeyi aynen kullanabilirsiniz.
]

def load_university_list() -> list:
    for attempt in range(1, 4):
        try:
            r = _session.get(UNIVERSITY_LIST_URL, timeout=TIMEOUT)
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
                log.info(f"Loaded {len(data)} universities from local cache.")
                return data
        except Exception as e:
            log.warning(f"Local cache read failed: {e}")

    log.warning(f"Using hardcoded fallback list.")
    return FALLBACK_UNIVERSITY_LIST

def match_university(name: str, ulist: list) -> tuple:
    name_norm       = normalize_for_match(clean_cell(name))
    name_norm_nsp   = name_norm.replace(" ", "").replace("-", "")

    best, best_len = None, 0

    for uni in ulist:
        u_norm     = normalize_for_match(uni["Name"])
        u_norm_nsp = u_norm.replace(" ", "").replace("-", "")

        matched = u_norm in name_norm or name_norm in u_norm
        if not matched:
            matched = u_norm_nsp in name_norm_nsp or name_norm_nsp in u_norm_nsp

        if matched and len(u_norm) > best_len:
            best, best_len = uni, len(u_norm)

    if best: return best["Name"], best["City"], best["Type"]

    log.warning(f"  No university match found for: '{name.strip()}'")
    return tr_upper(name.strip()), "Bilinmiyor", "Devlet"

# ── Existing JSON (deduplication) ─────────────────────────────────────────────

def load_existing_ads() -> tuple[list, set]:
    if not os.path.exists(OUTPUT_FILE): return [], set()
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

def normalize_title(raw: str) -> str:
    titles = extract_titles_from_cell(raw)
    return titles[0] if titles else tr_upper(raw.strip())

def extract_titles_from_cell(raw: str) -> list:
    found = []
    parts = re.split(r"[/,;]| ve | veya ", raw, flags=re.IGNORECASE)
    for part in parts:
        part = part.strip()
        if not part: continue
        part_up = tr_upper(part)
        matched = None
        for alias in sorted(TITLE_ALIASES, key=len, reverse=True):
            if tr_upper(alias) in part_up:
                matched = TITLE_ALIASES[alias]
                break
        if not matched:
            for t in ACADEMIC_TITLES:
                if tr_upper(t) in part_up:
                    matched = t
                    break
        if matched and matched not in found:
            found.append(matched)
    return found

def is_academic(title: str) -> bool:
    return title in ACADEMIC_TITLES

def extract_university_from_link_text(link_text: str) -> str:
    cleaned = clean_cell(link_text)
    up = tr_upper(cleaned)
    for marker in [
        tr_upper("REKTÖRLÜĞÜNDEN"), tr_upper("REKTÖRLÜĞÜ"),
        tr_upper("REKTORLUGUNDEN"), tr_upper("REKTORLUGU"),
        tr_upper("DÜZELTME İLAN"), tr_upper("DUZELTME ILAN"),
    ]:
        if marker in up: return up.split(marker)[0].strip()
    return up

def extract_university_from_text(text: str, ulist: list) -> str:
    text_norm     = normalize_for_match(text)
    text_norm_nsp = text_norm.replace(" ", "")
    best, best_len = None, 0

    for uni in ulist:
        u_norm     = normalize_for_match(uni["Name"])
        u_norm_nsp = u_norm.replace(" ", "").replace("-", "")
        if (u_norm in text_norm or u_norm_nsp in text_norm_nsp) and len(u_norm) > best_len:
            best, best_len = uni["Name"], len(u_norm)

    return best if best else "Bilinmiyor"

def extract_deadline(text: str, publish_date: datetime) -> str | None:
    m = re.search(r"son\s+başvuru\s+tarih\w*[:\s]*(\d{1,2})[./](\d{2})[./](\d{4})", text, re.IGNORECASE)
    if not m:
        for candidate in re.finditer(r"başvuru\s+tarih\w*[:\s]*(\d{1,2})[./](\d{2})[./](\d{4})", text, re.IGNORECASE):
            try:
                cd = datetime(int(candidate.group(3)), int(candidate.group(2)), int(candidate.group(1)), tzinfo=timezone.utc)
                if cd > publish_date: m = candidate; break
            except ValueError: pass
    
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        tm = re.search(r"saat\s*(\d{1,2})[:\.](\d{2})", text[max(0, m.start()-20): m.start()+200], re.IGNORECASE)
        h, mi = (int(tm.group(1)), int(tm.group(2))) if tm else (23, 59)
        try: return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).isoformat()
        except ValueError: pass

    for mname, mnum in TR_MONTHS.items():
        for mx in re.finditer(rf"(\d{{1,2}})\s+{mname}\s+(\d{{4}})", text, re.IGNORECASE):
            d, y = int(mx.group(1)), int(mx.group(2))
            try:
                dt = datetime(y, mnum, d, 23, 59, tzinfo=timezone.utc)
                if dt > publish_date: return dt.isoformat()
            except ValueError: pass

    m2 = re.search(r"(?:ilan[ıi]n?\s+yay[ıi]m\w*\s+tarihi[nk]den\s+itibaren|ilan\s+tarihinden\s+itibaren)[^0-9]*(\d+)", text, re.IGNORECASE)
    if m2:
        days = int(m2.group(1))
        if 7 <= days <= 60:
            return (publish_date + timedelta(days=days)).replace(tzinfo=timezone.utc).isoformat()
    return None

ALES_EXEMPT_TITLES = {"PROFESÖR", "DOÇENT"}

def extract_ales(text: str, title: str = "") -> dict:
    r = {"alesRequired": False, "alesScore": None, "alesType": None}
    if title in ALES_EXEMPT_TITLES: return r
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
            if re.match(r"^\d+$", cu.strip()): continue
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
            base: dict = {}
            if "faculty" in col:
                v = row[col["faculty"]]
                if v: last_faculty = v
                base["faculty"] = last_faculty
            else:
                base["faculty"] = ""
            base["department"]   = row[col["dept"]]  if "dept"  in col else ""
            base["requirements"] = row[col["req"]]   if "req"   in col else ""
            base["count"]        = max(1, int(re.sub(r"\D","", row[col["count"]] or "1") or "1")) if "count" in col else 1
            raw_title_cell = row[col["title"]] if "title" in col else " ".join(row)
            title_list = extract_titles_from_cell(raw_title_cell)
            if not title_list:
                title_list = [t for t in ACADEMIC_TITLES if tr_upper(t) in tr_upper(" ".join(row))]
            if not title_list and not base["faculty"] and not base["department"]: continue
            if not title_list: title_list = [""]  
            for title in title_list:
                pos = dict(base)
                pos["title"] = title
                req_ctx = pos["requirements"]
                ales = extract_ales(req_ctx, title)
                if not ales["alesRequired"]: ales = extract_ales(full_text, title)
                lang = extract_language(req_ctx, title)
                if not lang["foreignLanguageRequired"]: lang = extract_language(full_text, title)
                pos.update(ales); pos.update(lang)
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
        m = re.search(r'\b(\d{1,2})\b', line)
        cnt = int(m.group(1)) if m else 1
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

# ── Gemini API (Yedek Plan) İşleyici ──────────────────────────────────────────
def parse_pdf_with_gemini(pdf_bytes: bytes, uni_name: str) -> dict | None:
    """
    pdfplumber'ın başarısız olduğu durumlarda PDF'i Gemini 1.5 Flash 
    modeline göndererek yapılandırılmış JSON çıktısı alır.
    """
    if not GEMINI_AVAILABLE or not GEMINI_API_KEY:
        log.warning(f"  [{uni_name}] Gemini API yapılandırması eksik, yedek plan atlandı.")
        return None

    log.info(f"  [{uni_name}] Klasik tarama kadro bulamadı. Gemini API (Yedek Plan) devreye giriyor...")
    
    prompt = """
    Sen bir akademik ilan analiz uzmanısın. Ekli PDF dosyası bir üniversitenin akademik personel alım ilanıdır.
    Lütfen bu PDF'i incele ve aşağıdaki JSON formatına tam olarak uyacak şekilde verileri çıkar.
    Sadece geçerli bir JSON objesi döndür, dışına hiçbir yorum yazma:
    {
      "positions": [
        {
          "faculty": "Fakülte veya Yüksekokul Adı",
          "department": "Bölüm veya Anabilim Dalı",
          "title": "PROFESÖR, DOÇENT, DR. ÖĞR. ÜYESİ, ÖĞRETİM GÖREVLİSİ veya ARAŞTIRMA GÖREVLİSİ (sadece bunlardan biri)",
          "count": 1,
          "requirements": "İlanın özel şartları ve açıklamaları",
          "alesRequired": true,
          "alesScore": 70,
          "alesType": "SAY",
          "foreignLanguageRequired": true,
          "foreignLanguageScore": 50,
          "foreignLanguageExam": "YÖKDİL"
        }
      ],
      "applicationDocuments": ["Özgeçmiş", "Diploma Fotokopisi"]
    }
    Eğer ilanda bu akademik kadrolardan hiçbiri yoksa, positions listesini boş bırak.
    """
    
    try:
        model = genai.GenerativeModel(
            'gemini-1.5-flash', 
            generation_config={"response_mime_type": "application/json"}
        )
        
        pdf_part = {
            "mime_type": "application/pdf",
            "data": pdf_bytes
        }
        
        response = model.generate_content([pdf_part, prompt])
        data = json.loads(response.text)
        
        found_count = len(data.get('positions', []))
        log.info(f"  ✓ Gemini API başarıyla {found_count} akademik pozisyon tespit etti.")
        
        # Ücretsiz katmandaki 15 RPM limitine takılmamak için koruyucu bekleme süresi
        time.sleep(2) 
        return data
        
    except Exception as e:
        log.error(f"  Gemini API Hatası: {e}")
        return None

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

    raw_from_link = extract_university_from_link_text(link_text)
    uni_name, city, uni_type = match_university(raw_from_link, ulist)
    if city == "Bilinmiyor":
        n2, c2, t2 = match_university(extract_university_from_text(full_text, ulist), ulist)
        if c2 != "Bilinmiyor": uni_name, city, uni_type = n2, c2, t2

    deadline  = extract_deadline(full_text, publish_date)
    
    # 1. Aşama: Klasik PDF Plumber ve Regex Analizi
    positions = extract_positions_from_tables(all_tables, full_text)
    if not positions: positions = extract_positions_from_text(full_text)
    positions = [p for p in positions if is_academic(p.get("title",""))]
    
    docs = extract_documents(full_text)

    # 2. Aşama: Gemini API Yedek Planı (Hibrit Geçiş)
    if not positions:
        gemini_data = parse_pdf_with_gemini(pdf_bytes, uni_name)
        
        if gemini_data and gemini_data.get("positions"):
            # Gemini'den dönen title'ları güvenlik amaçlı standartlaştır (büyük harf vs.)
            raw_positions = gemini_data["positions"]
            positions = []
            for p in raw_positions:
                t_upper = tr_upper(p.get("title", ""))
                if is_academic(t_upper):
                    p["title"] = t_upper
                    positions.append(p)
            
            # Gemini evraklarını mevcut evrak listesiyle tekilleştirerek birleştir
            if "applicationDocuments" in gemini_data:
                docs = list(set(docs + gemini_data["applicationDocuments"]))
                
    # Her iki yöntem de sonuç vermezse ilanı atla
    if not positions:
        log.info(f"  Skipping {uni_name} — no academic titles found."); return None

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
    if not budget_ok(): return []
    index_url = build_index_url(date)
    log.info(f"Checking {date.strftime('%Y-%m-%d')}: {index_url}")
    soup = fetch_html(index_url)
    if not soup:
        log.info("  No gazette page."); return []

    links: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        lt = a.get_text(strip=True)
        lt_up = tr_upper(lt)
        is_academic_link = (
            tr_upper("Rektörlüğünden") in lt_up or
            tr_upper("Düzeltme İlan")  in lt_up or
            (tr_upper("Rektörlüğü") in lt_up and tr_upper("Üniversite") in lt_up)
        )
        if is_academic_link:
            abs_url = resolve_url(a["href"], index_url)
            pdf_url = to_pdf_url(abs_url)
            if pdf_url in existing_urls:
                log.info(f"  Already known: {pdf_url}"); continue
            links.append((pdf_url, lt))

    log.info(f"  {len(links)} new academic links.")
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

    pdf_counter = [0]  
    new_ads: list = []
    today = datetime.now(timezone.utc)

    for i in range(DAYS_TO_CHECK):
        if not budget_ok(): break
        new_ads.extend(scrape_day(today - timedelta(days=i), ulist, existing_urls, pdf_counter))

    seen: set = set()
    unique_new: list = []
    for ad in new_ads:
        if ad["url"] not in seen:
            seen.add(ad["url"]); unique_new.append(ad)

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
