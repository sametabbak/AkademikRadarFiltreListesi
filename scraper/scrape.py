# -*- coding: utf-8 -*-

import requests
from bs4 import BeautifulSoup
import json, re, io, time, logging
from datetime import datetime, timezone, timedelta

from pdf2image import convert_from_bytes
import pytesseract

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

RG_BASE = "https://www.resmigazete.gov.tr"
OUTPUT_FILE = "ilanlar.json"
DAYS_TO_CHECK = 5

ACADEMIC_TITLES = [
    "PROFESÖR",
    "DOÇENT",
    "DR. ÖĞR. ÜYESİ",
    "ÖĞRETİM GÖREVLİSİ",
    "ARAŞTIRMA GÖREVLİSİ",
]

TITLE_ALIASES = {
    "PROF.": "PROFESÖR",
    "DOÇ.": "DOÇENT",
    "DR. ÖĞR.": "DR. ÖĞR. ÜYESİ",
    "ÖĞR. GÖR.": "ÖĞRETİM GÖREVLİSİ",
    "ARŞ. GÖR.": "ARAŞTIRMA GÖREVLİSİ",
}

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})

# ─────────────────────────────────────────────
def build_url(date):
    return f"{RG_BASE}/ilanlar/eskiilanlar/{date:%Y/%m}/{date:%Y%m%d}-4.htm"

def resolve(href, base):
    if href.startswith("http"): return href
    if href.startswith("/"): return RG_BASE + href
    return base.rsplit("/",1)[0] + "/" + href

def to_pdf(url):
    return url.replace(".htm",".pdf")

# ─────────────────────────────────────────────
def fetch_html(url):
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except:
        return None

def fetch_pdf(url):
    try:
        r = session.get(url, timeout=30)
        return r.content
    except:
        return None

# ─────────────────────────────────────────────
def tr_upper(s):
    return s.replace("i","İ").replace("ı","I").upper()

def clean_text(text):
    text = re.sub(r"-\n","",text)
    text = re.sub(r"\n"," ",text)
    text = re.sub(r"\s+"," ",text)
    return text.strip()

# ─────────────────────────────────────────────
def extract_titles(text):

    found = set()
    text_up = tr_upper(text)

    for alias, real in TITLE_ALIASES.items():
        if tr_upper(alias) in text_up:
            found.add(real)

    for t in ACADEMIC_TITLES:
        if tr_upper(t) in text_up:
            found.add(t)

    return list(found)

# ─────────────────────────────────────────────
def parse_pdf_ocr(pdf_bytes):

    try:
        images = convert_from_bytes(pdf_bytes, dpi=300)
    except Exception as e:
        log.warning(f"PDF → image hata: {e}")
        return ""

    all_text = []

    for img in images:
        text = pytesseract.image_to_string(
            img,
            lang="tur",
            config="--oem 3 --psm 6"
        )
        all_text.append(text)

    return "\n".join(all_text)

# ─────────────────────────────────────────────
def extract_positions(text):

    positions = []
    words = text.split()

    for i in range(len(words)):
        chunk = " ".join(words[i:i+20])
        titles = extract_titles(chunk)

        if titles:
            for t in titles:
                positions.append({
                    "title": t,
                    "department": "",
                    "faculty": "",
                    "count": 1,
                    "requirements": chunk[:200]
                })

    return positions

# ─────────────────────────────────────────────
def parse_pdf(pdf_bytes, publish_date, url):

    text = parse_pdf_ocr(pdf_bytes)

    if not text or len(text) < 50:
        return None

    text = clean_text(text)

    positions = extract_positions(text)

    if not positions:
        return None

    return {
        "university": "Bilinmiyor",
        "city": "Bilinmiyor",
        "uniType": "Devlet",
        "publishDate": publish_date.isoformat(),
        "deadline": None,
        "detectedTitles": list(set(p["title"] for p in positions)),
        "positions": positions,
        "url": url
    }

# ─────────────────────────────────────────────
def scrape_day(date):

    url = build_url(date)
    soup = fetch_html(url)

    if not soup:
        return []

    ads = []

    for a in soup.find_all("a", href=True):

        text = a.get_text()

        if "Rektörlüğünden" not in text:
            continue

        pdf_url = to_pdf(resolve(a["href"], url))

        log.info(f"PDF indiriliyor: {pdf_url}")

        pdf_bytes = fetch_pdf(pdf_url)
        if not pdf_bytes:
            continue

        ad = parse_pdf(pdf_bytes, date, pdf_url)

        if ad:
            ads.append(ad)
            log.info(f"✓ ilan bulundu")

    return ads

# ─────────────────────────────────────────────
def main():

    all_ads = []
    today = datetime.now(timezone.utc)

    for i in range(DAYS_TO_CHECK):
        ads = scrape_day(today - timedelta(days=i))
        all_ads.extend(ads)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "generatedAt": today.isoformat(),
            "count": len(all_ads),
            "ads": all_ads
        }, f, ensure_ascii=False, indent=2)

    log.info(f"Toplam ilan: {len(all_ads)}")

# ─────────────────────────────────────────────
if __name__ == "__main__":
    main()
