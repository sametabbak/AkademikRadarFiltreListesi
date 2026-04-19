# -*- coding: utf-8 -*-

import requests
from bs4 import BeautifulSoup
import json, re, io, logging
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

# ───────────────────────────────

def build_url(date):
    return f"{RG_BASE}/ilanlar/eskiilanlar/{date:%Y/%m}/{date:%Y%m%d}-4.htm"

def resolve(href, base):
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return RG_BASE + href
    return base.rsplit("/", 1)[0] + "/" + href

def to_pdf(url):
    return url.replace(".htm", ".pdf")

# ───────────────────────────────

def fetch_html(url):
    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log.warning(f"HTML error: {e}")
        return None

def fetch_pdf(url):
    try:
        r = session.get(url, timeout=60)
        return r.content
    except Exception as e:
        log.warning(f"PDF error: {e}")
        return None

# ───────────────────────────────

def tr_upper(s):
    return s.replace("i","İ").replace("ı","I").upper()

def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()

# ───────────────────────────────

def extract_titles(text):
    found = set()
    up = tr_upper(text)

    for a, r in TITLE_ALIASES.items():
        if tr_upper(a) in up:
            found.add(r)

    for t in ACADEMIC_TITLES:
        if tr_upper(t) in up:
            found.add(t)

    return list(found)

# ───────────────────────────────
# OCR ENGINE (KRİTİK KISIM)

def pdf_to_text(pdf_bytes):

    try:
        images = convert_from_bytes(pdf_bytes, dpi=300)
    except Exception as e:
        log.warning(f"PDF convert error: {e}")
        return ""

    texts = []

    for img in images:
        text = pytesseract.image_to_string(
            img,
            lang="tur",
            config="--oem 3 --psm 6"
        )
        texts.append(text)

    return "\n".join(texts)

# ───────────────────────────────

def extract_positions(text):

    positions = []
    words = text.split()

    for i in range(len(words)):
        chunk = " ".join(words[i:i+25])
        titles = extract_titles(chunk)

        for t in titles:
            positions.append({
                "title": t,
                "department": "",
                "faculty": "",
                "count": 1,
                "requirements": chunk[:200]
            })

    return positions

# ───────────────────────────────

def parse_pdf(pdf_bytes, date, url):

    text = pdf_to_text(pdf_bytes)

    if not text or len(text) < 50:
        return None

    text = clean(text)

    positions = extract_positions(text)

    if not positions:
        return None

    return {
        "university": "Bilinmiyor",
        "city": "Bilinmiyor",
        "uniType": "Devlet",
        "publishDate": date.isoformat(),
        "deadline": None,
        "detectedTitles": list(set(p["title"] for p in positions)),
        "positions": positions,
        "url": url
    }

# ───────────────────────────────

def scrape_day(date):

    url = build_url(date)
    soup = fetch_html(url)

    if not soup:
        return []

    results = []

    for a in soup.find_all("a", href=True):

        text = a.get_text()

        if "Rektörlüğünden" not in text:
            continue

        pdf_url = to_pdf(resolve(a["href"], url))

        log.info(f"PDF: {pdf_url}")

        pdf = fetch_pdf(pdf_url)
        if not pdf:
            continue

        parsed = parse_pdf(pdf, date, pdf_url)

        if parsed:
            results.append(parsed)
            log.info("✔ ilan bulundu")

    return results

# ───────────────────────────────

def main():

    all_data = []
    today = datetime.now(timezone.utc)

    for i in range(DAYS_TO_CHECK):
        all_data += scrape_day(today - timedelta(days=i))

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "generatedAt": today.isoformat(),
            "count": len(all_data),
            "ads": all_data
        }, f, ensure_ascii=False, indent=2)

    log.info(f"TOPLAM: {len(all_data)} ilan")

# ───────────────────────────────

if __name__ == "__main__":
    main()
