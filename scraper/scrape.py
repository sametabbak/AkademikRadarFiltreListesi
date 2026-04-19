# -*- coding: utf-8 -*-
"""
AkademikRadar Scraper (Enhanced Version)
"""

import requests
from bs4 import BeautifulSoup
import json, re, io, os, time, logging
from datetime import datetime, timezone, timedelta

import pdfplumber
import fitz  # PyMuPDF
from rapidfuzz import fuzz

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── CONFIG ─────────────────────────────────────────────────
MAX_RUNTIME_SECONDS = 20 * 60
MAX_PDFS_PER_RUN = 30

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 25
TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)

OUTPUT_FILE = "ilanlar.json"
DAYS_TO_CHECK = 5
RG_BASE = "https://www.resmigazete.gov.tr"

HEADERS = {"User-Agent": "Mozilla/5.0"}

ACADEMIC_TITLES = {
    "PROFESÖR","DOÇENT","DR. ÖĞR. ÜYESİ",
    "ÖĞRETİM GÖREVLİSİ","ARAŞTIRMA GÖREVLİSİ"
}

# ── TIME ───────────────────────────────────────────────────
_start_time = time.monotonic()

def budget_ok():
    return (time.monotonic() - _start_time) < MAX_RUNTIME_SECONDS

# ── HELPERS ────────────────────────────────────────────────
def tr_upper(s): return s.replace("i","İ").replace("ı","I").upper()

def normalize_for_match(s):
    return re.sub(r"[^A-Z]", "", tr_upper(s))

# ── MULTI PARSER ───────────────────────────────────────────
def extract_text_pymupdf(pdf_bytes):
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        return "\n".join(page.get_text() for page in doc)
    except:
        return ""

def extract_pdf_text(pdf_bytes):
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text1 = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except:
        text1 = ""

    text2 = extract_text_pymupdf(pdf_bytes)

    return max([text1, text2], key=len)

# ── FUZZY UNIVERSITY MATCH ─────────────────────────────────
def match_university(name, ulist):
    name_norm = normalize_for_match(name)
    best, score = None, 0

    for u in ulist:
        s = fuzz.partial_ratio(name_norm, normalize_for_match(u["Name"]))
        if s > score:
            best, score = u, s

    if best and score > 80:
        return best["Name"], best["City"], best["Type"]

    return name, "Bilinmiyor", "Devlet"

# ── VALIDATION ─────────────────────────────────────────────
def validate_ad(ad):
    if not ad["positions"]: return False
    for p in ad["positions"]:
        if p["count"] <= 0: return False
        if p["title"] not in ACADEMIC_TITLES: return False
    return True

# ── CONFIDENCE ─────────────────────────────────────────────
def compute_confidence(ad):
    score = 0
    if ad["positions"]: score += 0.3
    if ad["deadline"]: score += 0.2
    if ad["city"] != "Bilinmiyor": score += 0.2
    if ad["detectedTitles"]: score += 0.2
    if ad["applicationDocuments"]: score += 0.1
    return round(score,2)

# ── SIMPLE EXTRACTION (LIGHT VERSION) ──────────────────────
def extract_positions(text):
    positions = []
    for t in ACADEMIC_TITLES:
        if t in tr_upper(text):
            positions.append({
                "title": t,
                "count": 1,
                "department": "",
                "faculty": ""
            })
    return positions

def extract_deadline(text):
    m = re.search(r"(\d{1,2})[./](\d{2})[./](\d{4})", text)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None

# ── PDF PARSER ─────────────────────────────────────────────
def parse_pdf(pdf_bytes, link_text, publish_date, ulist):
    text = extract_pdf_text(pdf_bytes)

    uni, city, typ = match_university(link_text, ulist)
    positions = extract_positions(text)

    if not positions:
        return None

    ad = {
        "university": uni,
        "city": city,
        "uniType": typ,
        "publishDate": publish_date.isoformat(),
        "deadline": extract_deadline(text),
        "positions": positions,
        "detectedTitles": [p["title"] for p in positions],
        "applicationDocuments": [],
        "url": ""
    }

    if not validate_ad(ad):
        return None

    ad["confidence"] = compute_confidence(ad)

    return ad

# ── SCRAPER ────────────────────────────────────────────────
def fetch_html(url):
    try:
        r = requests.get(url, timeout=TIMEOUT)
        return BeautifulSoup(r.text, "html.parser")
    except:
        return None

def fetch_bytes(url):
    try:
        return requests.get(url, timeout=TIMEOUT).content
    except:
        return None

def build_index_url(date):
    return f"{RG_BASE}/ilanlar/eskiilanlar/{date.strftime('%Y/%m')}/{date.strftime('%Y%m%d')}-4.htm"

def scrape_day(date, ulist):
    soup = fetch_html(build_index_url(date))
    if not soup: return []

    ads = []
    for a in soup.find_all("a", href=True):
        if "Rektörlüğünden" in a.text:
            url = RG_BASE + "/" + a["href"]
            pdf_url = url.replace(".htm",".pdf")

            pdf = fetch_bytes(pdf_url)
            if not pdf: continue

            ad = parse_pdf(pdf, a.text, date, ulist)
            if ad:
                ad["url"] = pdf_url
                ads.append(ad)

    return ads

# ── MAIN ───────────────────────────────────────────────────
def main():
    log.info("Starting scraper")

    # minimal university list
    ulist = [
        {"Name":"ANKARA ÜNİVERSİTESİ","City":"Ankara","Type":"Devlet"},
        {"Name":"HACETTEPE ÜNİVERSİTESİ","City":"Ankara","Type":"Devlet"},
        {"Name":"İSTANBUL ÜNİVERSİTESİ","City":"İstanbul","Type":"Devlet"}
    ]

    all_ads = []
    today = datetime.now(timezone.utc)

    for i in range(DAYS_TO_CHECK):
        if not budget_ok(): break
        all_ads.extend(scrape_day(today - timedelta(days=i), ulist))

    output = {
        "generatedAt": today.isoformat(),
        "count": len(all_ads),
        "ads": all_ads
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    log.info(f"Done: {len(all_ads)} ads")

if __name__ == "__main__":
    main()
