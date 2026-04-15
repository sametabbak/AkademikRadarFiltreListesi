"""
AkademikRadar Scraper — fixed version
Fixes:
  1. Turkish uppercase (i→İ, ı→I) for all comparisons
  2. PDF text cleaning (broken spaces, paragraph-break artefacts)
  3. Deduplication against existing ilanlar.json
  4. Only academic titles kept; non-academic ads (security etc.) filtered out
  5. Exam calendar fetched from ÖSYM website
  6. Workflow runs at 07:00 Turkey time (04:00 UTC, cron '0 4 * * *')
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

OUTPUT_FILE = "ilanlar.json"  # always written to the repo root (cwd)
DAYS_TO_CHECK     = 20
RG_BASE           = "https://www.resmigazete.gov.tr"
UNIVERSITY_LIST_URL = (
    "https://raw.githubusercontent.com/sametabbak/AkademikRadarFiltreListesi"
    "/refs/heads/main/TurkishUniversityList"
)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# Only these are accepted as valid academic job titles.
# Ads with none of these in their positions are discarded.
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

# ── Turkish-aware string helpers ─────────────────────────────────────────────

def tr_upper(s: str) -> str:
    """Turkish-correct uppercase: i→İ, ı→I (not the English i→I)."""
    return s.replace("i", "İ").replace("ı", "I").upper()


def normalize_for_match(s: str) -> str:
    """
    Flatten ALL Turkish/ASCII dotted-I variants to a single ASCII form so
    strings from different sources (PDF vs JSON list) compare equal
    regardless of font encoding.

    PDFs commonly store İ as plain I (ASCII 73) due to font limitations.
    The university list stores canonical İ (U+0130).
    We collapse both to I so matching works either way.
    Also flattens ğ,ş,ç,ö,ü to ASCII for maximum robustness.
    """
    return (
        s
        .replace("İ", "I")
        .replace("ı", "I")
        .replace("i", "I")
        .replace("ğ", "g").replace("Ğ", "G")
        .replace("ş", "s").replace("Ş", "S")
        .replace("ç", "c").replace("Ç", "C")
        .replace("ö", "o").replace("Ö", "O")
        .replace("ü", "u").replace("Ü", "U")
        .upper()
    )


def clean_cell(value: str) -> str:
    if not value:
        return ""
    text = re.sub(r"[\r\n]+", " ", value)
    text = re.sub(r"[ \t]{2,}", " ", text)
    # Insert spaces at known Turkish suffix boundaries
    for suffix in ["ÜNİVERSİTESİ", "ÜNİVERSİTESI", "FAKÜLTESİ", "ENSTİTÜSÜ",
                   "YÜKSEKOKULU", "REKTÖRLÜĞÜNDEN", "BÖLÜMÜ", "PROGRAMI"]:
        text = re.sub(rf"({re.escape(suffix)})([A-ZÇĞİÖŞÜ])", r"\1 \2", text)
    return text.strip()


def tr_contains(haystack: str, needle: str) -> bool:
    return normalize_for_match(needle) in normalize_for_match(haystack)

# ── University list ───────────────────────────────────────────────────────────
def load_university_list() -> list:
    try:
        r = requests.get(UNIVERSITY_LIST_URL, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        log.info(f"Loaded {len(data)} universities.")
        return data
    except Exception as e:
        log.error(f"University list load failed: {e}")
        return []

def match_university(name: str, ulist: list) -> tuple:
    """
    Match a raw university name (possibly from PDF, with encoding artefacts)
    against the university list.  Uses normalize_for_match() so that
    'ÜNIVERSITESI' (PDF, I) matches 'ÜNİVERSİTESİ' (list, İ).
    Also cleans embedded line breaks before matching.
    """
    name_clean = clean_cell(name)          # remove \r\n inside the name
    name_norm  = normalize_for_match(name_clean)
    best, best_len = None, 0
    for uni in ulist:
        u_norm = normalize_for_match(uni["Name"])
        if u_norm in name_norm or name_norm in u_norm:
            if len(u_norm) > best_len:
                best, best_len = uni, len(u_norm)
    if best:
        return best["Name"], best["City"], best["Type"]
    return tr_upper(name_clean), "Bilinmiyor", "Devlet"

# ── HTTP helpers ──────────────────────────────────────────────────────────────
def fetch_html(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log.warning(f"HTML fetch failed [{url}]: {e}")
        return None

def fetch_bytes(url, retries=3):
    for attempt in range(1, retries+1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.content
        except Exception as e:
            log.warning(f"Bytes fetch attempt {attempt} failed [{url}]: {e}")
            if attempt < retries: time.sleep(2)
    return None

# ── URL helpers ───────────────────────────────────────────────────────────────
def build_index_url(date):
    return f"{RG_BASE}/ilanlar/eskiilanlar/{date.strftime('%Y')}/{date.strftime('%m')}/{date.strftime('%Y%m%d')}-4.htm"

def resolve_url(href, index_url):
    if href.startswith("http"): return href
    if href.startswith("/"): return RG_BASE + href
    return index_url.rsplit("/", 1)[0] + "/" + href

def to_pdf_url(url):
    if url.endswith(".htm"): return url[:-4] + ".pdf"
    if url.endswith(".pdf"): return url
    return url + ".pdf"

# ── PDF text cleaner ──────────────────────────────────────────────────────────
def clean_pdf_text(raw: str) -> str:
    # Merge hyphenated line breaks
    text = re.sub(r"-\n", "", raw)
    # Single newlines that are NOT paragraph breaks → space
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    # Collapse multiple spaces
    text = re.sub(r"[ \t]{2,}", " ", text)

    # ── Insert missing spaces between run-together uppercase words ────────────
    # Strategy 1: lowercase→uppercase boundary (original, still needed)
    text = re.sub(r"([a-zçğışöüA-ZÇĞİÖŞÜ])([A-ZÇĞİÖŞÜ]{2,})", r"\1 \2", text)

    # Strategy 2: known Turkish word-ending suffixes followed immediately by
    # another word — covers ALL-CAPS run-together strings like
    # "HACETTEPЕÜNIVERSITESI" or "TIBBIYEFAKULTESI"
    suffixes = [
        r"(ÜNİVERSİTESİ)([A-ZÇĞİÖŞÜ])",
        r"(ÜNİVERSİTESI)([A-ZÇĞİÖŞÜ])",   # PDF encoding variant
        r"(FAKÜLTESİ)([A-ZÇĞİÖŞÜ])",
        r"(FAKULTESİ)([A-ZÇĞİÖŞÜ])",
        r"(ENSTİTÜSÜ)([A-ZÇĞİÖŞÜ])",
        r"(YÜKSEKOKULU)([A-ZÇĞİÖŞÜ])",
        r"(MÜDÜRLÜĞÜ)([A-ZÇĞİÖŞÜ])",
        r"(REKTÖRLÜĞÜNDEN)([A-ZÇĞİÖŞÜ])",
        r"(REKTORLUGUNDEN)([A-ZÇĞİÖŞÜ])",
        r"(ANABİLİM DALI)([A-ZÇĞİÖŞÜ])",
        r"(BÖLÜMÜ)([A-ZÇĞİÖŞÜ])",
        r"(PROGRAMI)([A-ZÇĞİÖŞÜ])",
        r"(TEKNİK)([A-ZÇĞİÖŞÜ])",
        r"(ÜNİVERSİTESİ)(REKTÖRLÜĞÜNDEN)",
    ]
    for pattern in suffixes:
        text = re.sub(pattern, r"\1 \2", text)

    # Strategy 3: split on digit→letter and letter→digit boundaries
    text = re.sub(r"(\d)([A-ZÇĞİÖŞÜa-zçğışöü])", r"\1 \2", text)
    text = re.sub(r"([A-ZÇĞİÖŞÜa-zçğışöü])(\d)", r"\1 \2", text)

    # Final cleanup
    text = re.sub(r"[ \t]{2,}", " ", text)
    lines = [l.strip() for l in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

# ── Existing JSON loader (deduplication) ─────────────────────────────────────
def load_existing_ads():
    # When OUTPUT_DIR is set the existing file lives in the target repo checkout.
    if not os.path.exists(OUTPUT_FILE):
        return [], set()
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        ads = data.get("ads", [])
        urls = {ad["url"] for ad in ads if "url" in ad}
        log.info(f"Loaded {len(ads)} existing ads, {len(urls)} unique URLs.")
        return ads, urls
    except Exception as e:
        log.warning(f"Could not read {OUTPUT_FILE}: {e}")
        return [], set()

# ── Title helpers ─────────────────────────────────────────────────────────────
def normalize_title(raw: str) -> str:
    raw_up = tr_upper(raw.strip())
    for alias, canonical in TITLE_ALIASES.items():
        if tr_upper(alias) in raw_up:
            return canonical
    for title in ACADEMIC_TITLES:
        if tr_upper(title) in raw_up:
            return title
    return raw_up

def is_academic(title: str) -> bool:
    return title in ACADEMIC_TITLES

# ── Extraction helpers ────────────────────────────────────────────────────────
def extract_university_from_link_text(link_text: str) -> str:
    """
    Extract university name from Resmî Gazete link text such as
    'BAŞKENT ÜNİVERSİTESİ REKTÖRLÜĞÜNDEN'.
    Cleans embedded line breaks first, then splits on REKTÖRLÜĞÜNDEN.
    """
    cleaned = clean_cell(link_text)
    up = tr_upper(cleaned)
    marker = tr_upper("REKTÖRLÜĞÜNDEN")
    if marker in up:
        return up.split(marker)[0].strip()
    return up

def extract_university_from_text(text: str, ulist: list) -> str:
    """
    Scan full PDF text for a university name.
    Uses normalize_for_match() so PDF encoding differences (I vs İ) don't
    cause misses.
    """
    text_norm = normalize_for_match(text)
    best, best_len = None, 0
    for uni in ulist:
        u_norm = normalize_for_match(uni["Name"])
        if u_norm in text_norm and len(u_norm) > best_len:
            best, best_len = uni["Name"], len(u_norm)
    if best: return best
    # Fallback: extract pattern before REKTORLUGUNDEN (normalised)
    m = re.search(r"([\wCGIOSUcgiosu\s]+?)\s*REKTORLUGUNDEN", text_norm)
    if m:
        candidate = m.group(1).strip()
        for uni in ulist:
            if normalize_for_match(uni["Name"]) in candidate or candidate in normalize_for_match(uni["Name"]):
                return uni["Name"]
        return candidate
    return "Bilinmiyor"

def extract_deadline(text: str, publish_date: datetime):
    # Pattern 1: explicit "Son Başvuru Tarihi: DD.MM.YYYY"
    m = re.search(r"son\s+başvuru\s+tarih\w*[:\s]*(\d{1,2})[./](\d{2})[./](\d{4})", text, re.IGNORECASE)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        tm = re.search(r"saat\s*(\d{1,2})[:\.](\d{2})", text[max(0, m.start()-20):m.start()+200], re.IGNORECASE)
        h, mi = (int(tm.group(1)), int(tm.group(2))) if tm else (23, 59)
        try: return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).isoformat()
        except ValueError: pass
    # Pattern 2: Turkish month name after publish_date
    for mname, mnum in TR_MONTHS.items():
        for m in re.finditer(rf"(\d{{1,2}})\s+{mname}\s+(\d{{4}})", text, re.IGNORECASE):
            d, y = int(m.group(1)), int(m.group(2))
            try:
                dt = datetime(y, mnum, d, 23, 59, tzinfo=timezone.utc)
                if dt > publish_date: return dt.isoformat()
            except ValueError: pass
    # Pattern 3: relative "X gün"
    m = re.search(r"(?:ilan[ıi]n?\s+yay[ıi]m\w*\s+tarihi[nk]den\s+itibaren|ilan\s+tarihinden\s+itibaren)[^0-9]*(\d+)", text, re.IGNORECASE)
    if m:
        days = int(m.group(1))
        if 7 <= days <= 60:
            return (publish_date + timedelta(days=days)).replace(tzinfo=timezone.utc).isoformat()
    return None

def extract_ales(text):
    r = {"alesRequired": False, "alesScore": None, "alesType": None}
    if "ALES" not in tr_upper(text): return r
    r["alesRequired"] = True
    m = re.search(r"ALES[^0-9\n]{0,60}?(\d{2,3})\s*(?:ve üzeri|veya üzeri|puan|puanı)", text, re.IGNORECASE)
    if not m: m = re.search(r"en\s+az\s*(\d{2,3})\s*(?:ALES|puan)", text, re.IGNORECASE)
    if m: r["alesScore"] = int(m.group(1))
    tm = re.search(r"ALES[^()\n]{0,60}?\b(SAY|SÖZ|EA|DİL|DIL)\b", text, re.IGNORECASE)
    if tm: r["alesType"] = tm.group(1).upper().replace("DIL", "DİL")
    return r

def extract_language(text):
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

def extract_documents(text):
    docs, text_l = [], text.lower()
    for kw, label in [
        ("özgeçmiş", "Özgeçmiş"), ("nüfus cüzdan", "Nüfus Cüzdanı Sureti"),
        ("diploma", "Diploma Fotokopisi"), ("ales belgesi", "ALES Belgesi"),
        ("ales sonuç", "ALES Sonuç Belgesi"), ("yds belgesi", "YDS Belgesi"),
        ("yokdil belgesi", "YÖKDİL Belgesi"), ("yabancı dil belgesi", "Yabancı Dil Belgesi"),
        ("fotoğraf", "Vesikalık Fotoğraf"), ("askerlik", "Askerlik Durum Belgesi"),
        ("transkript", "Transkript (Not Döküm Belgesi)"), ("not döküm", "Transkript (Not Döküm Belgesi)"),
        ("yayın listesi", "Yayın Listesi"), ("sabıka", "Sabıka Kaydı"),
        ("doktora belgesi", "Doktora Belgesi"), ("doçentlik belgesi", "Doçentlik Belgesi"),
        ("başvuru dilekçe", "Başvuru Dilekçesi"), ("öğrenci belgesi", "Öğrenci Belgesi"),
    ]:
        if kw in text_l and label not in docs: docs.append(label)
    return docs

# ── Table / text position extractors ─────────────────────────────────────────
FACULTY_KEYS = ["FAKÜLTESİ","YÜKSEKOKUL","ENSTİTÜSÜ","MYO","MESLEK","BİRİM","OKUL"]
DEPT_KEYS    = ["ANABİLİM","PROGRAM","BÖLÜM","DAL","ALAN"]
TITLE_KEYS   = ["UNVAN","ÜNVAN","KADRO ÜNVANI","POZİSYON","ÜNVANI"]
COUNT_KEYS   = ["SAYI","ADET","KADRO ADEDİ","KADRO SAYISI"]
REQ_KEYS     = ["AÇIKLAMA","NİTELİK","ÖZEL ŞART","ARANAN ŞART","KOŞUL","NİTELİKLER"]

def extract_positions_from_tables(tables, full_text):
    positions = []

    FACULTY_KEYS = ["FAKÜLTESİ", "YÜKSEKOKUL", "ENSTİTÜSÜ", "MYO", "MESLEK",
                    "BİRİM", "OKUL", "MERKEZ"]
    DEPT_KEYS = ["ANABİLİM", "PROGRAM", "BÖLÜM", "DAL", "ALAN"]
    TITLE_KEYS = ["UNVAN", "ÜNVAN", "KADRO ÜNVANI", "POZİSYON", "ÜNVANI"]
    COUNT_KEYS = ["SAYI", "ADET", "KADRO ADEDİ", "KADRO SAYISI"]
    REQ_KEYS = ["AÇIKLAMA", "NİTELİK", "ÖZEL ŞART", "ARANAN ŞART", "KOŞUL",
                "AÇIKLAMALAR", "NİTELİKLER"]

    for table in tables:
        if not table or len(table) < 2: continue
        header_idx = None
        for i, row in enumerate(table[:5]):
            row_up = tr_upper(" ".join(str(c or "") for c in row))
            hits = sum(1 for k in TITLE_KEYS + COUNT_KEYS + FACULTY_KEYS if tr_upper(k) in row_up)
            if hits >= 2: header_idx = i; break
        if header_idx is None: continue

        header = table[header_idx]
        col = {}
        for j, cell in enumerate(header):
            cu = tr_upper(str(cell or "").strip())
            if "faculty"      not in col and any(tr_upper(k) in cu for k in FACULTY_KEYS): col["faculty"] = j
            elif "department" not in col and any(tr_upper(k) in cu for k in DEPT_KEYS):    col["department"] = j
            elif "title"      not in col and any(tr_upper(k) in cu for k in TITLE_KEYS):   col["title"] = j
            elif "count"      not in col and any(tr_upper(k) in cu for k in COUNT_KEYS):   col["count"] = j
            elif "req"        not in col and any(tr_upper(k) in cu for k in REQ_KEYS):     col["req"] = j
        if not col: continue

        # Track last seen faculty (many tables have merged faculty cells)
        last_faculty = ""
        for row in table[header_idx+1:]:
            if not row or not any(row): continue
            # Apply clean_cell to every cell — removes \r\n inside cell values
            row = [clean_cell(str(c or "")) for c in row]
            pos = {}
            if "faculty" in col:
                v = row[col["faculty"]]
                if v: last_faculty = v
                pos["faculty"] = last_faculty
            else: pos["faculty"] = ""
            pos["department"]  = row[col["department"]] if "department" in col else ""
            pos["requirements"] = row[col["req"]] if "req" in col else ""
            pos["count"] = max(1, int(re.sub(r"\D","",row[col["count"]] or "1") or "1")) if "count" in col else 1
            if "title" in col:
                pos["title"] = normalize_title(row[col["title"]])
            else:
                ru = tr_upper(" ".join(str(c or "") for c in row))
                pos["title"] = next((t for t in ACADEMIC_TITLES if tr_upper(t) in ru), "")
            if not pos["title"] and not pos["faculty"] and not pos["department"]: continue
            ctx = pos["requirements"] + "\n" + full_text
            pos.update(extract_ales(ctx)); pos.update(extract_language(ctx))
            positions.append(pos)

    return positions

def extract_positions_from_text(full_text):
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
        pos.update(extract_ales(req+"\n"+full_text)); pos.update(extract_language(req+"\n"+full_text))
        positions.append(pos)

    return positions

def generate_snippet(university, positions, deadline):
    if not positions: return f"{university} akademik personel alım ilanı."
    tc = {}
    for p in positions:
        t = p.get("title","")
        if is_academic(t): tc[t] = tc.get(t,0) + p.get("count",1)
    summary = ", ".join(f"{c} {t}" for t,c in tc.items())
    faculties = list(dict.fromkeys(p.get("faculty","") for p in positions if p.get("faculty")))
    fac_str = ", ".join(faculties[:3]) + (f" ve {len(faculties)-3} birim daha" if len(faculties)>3 else "")
    snippet = f"{university} bünyesine {summary} alınacaktır."
    if fac_str: snippet += f" Birimler: {fac_str}."
    if deadline:
        try:
            dt = datetime.fromisoformat(deadline)
            snippet += f" Son başvuru: {dt.strftime('%d.%m.%Y')}."
        except: pass
    return snippet

# ── PDF parser ────────────────────────────────────────────────────────────────
def parse_pdf(pdf_bytes, link_text, publish_date, ulist):
    if not PDF_AVAILABLE: return None
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            raw_pages = [page.extract_text() or "" for page in pdf.pages]
            all_tables = []
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
        pdf_uni = extract_university_from_text(full_text, ulist)
        n2, c2, t2 = match_university(pdf_uni, ulist)
        if c2 != "Bilinmiyor": uni_name, city, uni_type = n2, c2, t2

    deadline  = extract_deadline(full_text, publish_date)
    positions = extract_positions_from_tables(all_tables, full_text)
    if not positions: positions = extract_positions_from_text(full_text)

    # Filter to academic only — discard security guards, admin, etc.
    positions = [p for p in positions if is_academic(p.get("title",""))]
    if not positions:
        log.info(f"  Skipping {uni_name} — no academic titles (non-academic ad).")
        return None

    docs     = extract_documents(full_text)
    snippet  = generate_snippet(uni_name, positions, deadline)
    detected = list(dict.fromkeys(p["title"] for p in positions if is_academic(p["title"])))

    return {
        "university":            uni_name,
        "city":                  city,
        "uniType":               uni_type,
        "publishDate":           publish_date.isoformat(),
        "deadline":              deadline,
        "detectedTitles":        detected,
        "contentSnippet":        snippet,
        "positions":             positions,
        "applicationDocuments":  docs,
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
    exams = []
    for url in ["https://www.osym.gov.tr/TR,6/sinav-takvimi.html","https://www.osym.gov.tr/TR,6/"]:
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
            # Try DD.MM.YYYY
            dm = re.search(r"(\d{1,2})[.\-/](\d{2})[.\-/](\d{4})", row_text)
            if dm:
                try:
                    dt = datetime(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))
                    exams.append({"shortName":f"{matched_name} {dt.year}","name":cells[0],
                                  "field":meta["field"],"examDate":dt.strftime("%Y-%m-%d"),
                                  "year":dt.year,"officialUrl":meta["url"]})
                    continue
                except ValueError: pass
            # Try "DD Month YYYY"
            for mname, mnum in TR_MONTHS.items():
                m2 = re.search(rf"(\d{{1,2}})\s+{mname}\s+(\d{{4}})", row_text, re.IGNORECASE)
                if m2:
                    try:
                        dt = datetime(int(m2.group(2)), mnum, int(m2.group(1)))
                        exams.append({"shortName":f"{matched_name} {dt.year}","name":cells[0],
                                      "field":meta["field"],"examDate":dt.strftime("%Y-%m-%d"),
                                      "year":dt.year,"officialUrl":meta["url"]})
                    except ValueError: pass
                    break
        if exams: break
    if exams: log.info(f"Fetched {len(exams)} exam dates.")
    else:      log.warning("Could not fetch exam calendar — section will be empty.")
    return exams

# ── Day scraping ──────────────────────────────────────────────────────────────
def scrape_day(date, ulist, existing_urls):
    index_url = build_index_url(date)
    log.info(f"Checking {date.strftime('%Y-%m-%d')}: {index_url}")
    soup = fetch_html(index_url)
    if not soup: log.info("  No gazette page."); return []

    links = []
    for a in soup.find_all("a", href=True):
        lt = a.get_text(strip=True)
        if "Rektörlüğünden" in lt:
            abs_url = resolve_url(a["href"], index_url)
            pdf_url = to_pdf_url(abs_url)
            if pdf_url in existing_urls:
                log.info(f"  Already known, skipping: {pdf_url}"); continue
            links.append((pdf_url, lt))

    log.info(f"  {len(links)} new links.")
    ads = []
    for pdf_url, lt in links:
        time.sleep(0.8)
        pdf_bytes = fetch_bytes(pdf_url)
        if not pdf_bytes: log.warning(f"  Download failed: {pdf_url}"); continue
        parsed = parse_pdf(pdf_bytes, lt, date, ulist)
        if parsed is None: continue
        parsed["url"] = pdf_url
        ads.append(parsed)
        log.info(f"  ✓ {parsed['university']} — {parsed['detectedTitles']}")
    return ads

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=== AkademikRadar Scraper Starting ===")

    if not PDF_AVAILABLE:
        log.error("pdfplumber not installed."); raise SystemExit(1)

    ulist = load_university_list()
    existing_ads, existing_urls = load_existing_ads()

    all_ads: list[dict] = []
    today = datetime.now(timezone.utc)
    new_ads = []
    for i in range(DAYS_TO_CHECK):
        new_ads.extend(scrape_day(today - timedelta(days=i), ulist, existing_urls))

    # Deduplicate new ads
    seen, unique_new = set(), []
    for ad in new_ads:
        if ad["url"] not in seen:
            seen.add(ad["url"]); unique_new.append(ad)

    # Merge new + existing, prune >90 days
    cutoff = today - timedelta(days=90)
    all_ads = unique_new + existing_ads
    all_ads = [
        ad for ad in all_ads
        if datetime.fromisoformat(ad.get("publishDate", today.isoformat())).replace(tzinfo=timezone.utc) >= cutoff
    ]
    all_ads.sort(key=lambda x: x.get("publishDate",""), reverse=True)

    exam_calendar = fetch_exam_calendar()

    output = {"generatedAt": today.isoformat(), "count": len(all_ads),
              "ads": all_ads, "examCalendar": exam_calendar}

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    log.info(f"=== Done. {len(unique_new)} new + {len(existing_ads)} kept = {len(all_ads)} total ===")

if __name__ == "__main__":
    main()
