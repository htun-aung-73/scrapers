"""Walk BNI news listing pages, then fetch each story for full details."""

import csv
import time

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.bnionline.net/en/news"
PAGES = 3
SLEEP_SECONDS = 0.4
OUTPUT_CSV = "articles.csv"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )
}


def parse_listing_page(html):
    soup = BeautifulSoup(html, "lxml")
    records = []
    for card in soup.select("div.card"):
        title_link = card.select_one(".card-title a")
        date_span = card.select_one(".card-date span[content]")
        category_link = card.select_one(".card-category a")

        if not title_link or not date_span:
            continue

        records.append(
            {
                "url": title_link["href"],
                "date": date_span["content"][:10],
                "outlet": category_link.get_text(strip=True) if category_link else None,
            }
        )
    return records


def collect_listing(pages=PAGES):
    records = []
    for page in range(pages):
        resp = requests.get(BASE_URL, params={"page": page}, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        records.extend(parse_listing_page(resp.text))
        time.sleep(SLEEP_SECONDS)
    return records


def parse_article_page(html):
    soup = BeautifulSoup(html, "lxml")

    title_meta = soup.select_one('meta[property="og:title"]')
    title = title_meta["content"] if title_meta else None

    paragraphs = soup.select(".field-name-body .field-item p")
    body = "\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))

    outlet_link = soup.select_one(".ethnic-media-name a")
    outlet = outlet_link.get_text(strip=True) if outlet_link else None

    return {"title": title, "body": body, "outlet": outlet}


def enrich_record(record):
    resp = requests.get(record["url"], headers=HEADERS, timeout=15)
    resp.raise_for_status()
    article = parse_article_page(resp.text)

    record["title"] = article["title"]
    record["body"] = article["body"]
    record["words"] = len(article["body"].split())
    if not record["outlet"]:
        record["outlet"] = article["outlet"]

    return record


def main():
    records = collect_listing()

    for record in records:
        enrich_record(record)
        time.sleep(SLEEP_SECONDS)

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "outlet", "title", "words", "url", "body"])
        writer.writeheader()
        writer.writerows(records)

    named_outlet_count = sum(1 for r in records if r["outlet"])
    print(f"Wrote {len(records)} rows to {OUTPUT_CSV}")
    print(f"Rows with a named outlet: {named_outlet_count}")


if __name__ == "__main__":
    main()
