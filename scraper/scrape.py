# -*- coding: utf-8 -*-

import requests
from bs4 import BeautifulSoup
import json, re, io, os, time, logging
from datetime import datetime, timezone, timedelta

import pdfplumber

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
MAX_RUNTIME_SECONDS = 20 * 60
MAX_PDFS_PER_RUN = 30

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 25
TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)

OUTPUT_FILE = "ilanlar.json"
DAYS_TO_CHECK = 5
RG_BASE = "https://www.resmigazete.gov.tr"

HEADERS = {"User-Agent": "Mozilla/5.0"}

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

# ─────────────────────────────────────────────
start_time = time.monotonic()

def budget_ok():
    return (time.monotonic() - start_time) < MAX_RUNTIME_SECONDS

# ─────────────────────────────────────────────
def tr_upper(s):
    return s.replace("i","İ").replace("ı","I").upper()

def normalize(s):
    return (
        s.replace("İ","I").replace("ı","I")
        .replace("ğ","g").replace("ş","s")
        .replace("ç","c").replace("ö","o").replace("ü","u")
        .upper()
    )

def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()

# ─────────────────────────────────────────────
session = requests.Session()
session.headers.update(HEADERS)

def fetch_html(url):
    try:
        r = session.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except:
        return None

def fetch_pdf(url):
    try:
        r = session.get(url, timeout=TIMEOUT)
        return r.content
    except:
        return None

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
def extract_titles(text):
    found = []
    text_up = tr_upper(text)

    for alias, real in TITLE_ALIASES.items():
        if tr_upper(alias) in text_up:
            found.append(real)

    for t in ACADEMIC_TITLES:
        if tr_upper(t) in text_up:
            found.append(t)

    return list(set(found))

# ─────────────────────────────────────────────
def clean_pdf_text(text):
    text = re.sub(r"-\n","",text)
    text = re.sub(r"\n"," ",text)
    return clean(text)

# ─────────────────────────────────────────────
def extract_positions(text):
    positions = []

    lines = text.split(" ")

    for i in range(len(lines)):
        chunk = " ".join(lines[i:i+15])
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

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except:
        return None

    text = clean_pdf_text(text)

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

        link = to_pdf(resolve(a["href"], url))

        pdf = fetch_pdf(link)
        if not pdf:
            continue

        ad = parse_pdf(pdf, date, link)

        if ad:
            ads.append(ad)
            log.info(f"✓ bulundu: {link}")

    return ads

# ─────────────────────────────────────────────
def main():

    all_ads = []
    today = datetime.now(timezone.utc)

    for i in range(DAYS_TO_CHECK):
        if not budget_ok():
            break

        ads = scrape_day(today - timedelta(days=i))
        all_ads.extend(ads)

    with open(OUTPUT_FILE,"w",encoding="utf-8") as f:
        json.dump({
            "generatedAt": today.isoformat(),
            "count": len(all_ads),
            "ads": all_ads
        }, f, ensure_ascii=False, indent=2)

    log.info(f"Toplam ilan: {len(all_ads)}")

# ─────────────────────────────────────────────
if __name__ == "__main__":
    main()
