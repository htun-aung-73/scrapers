"""Turn articles.csv into structured township-level event rows using the Gemini API."""

import csv
import json
import os
import re
import sys
import time

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

INPUT_CSV = "articles.csv"
OUTPUT_CSV = "extracted.csv"
# gemini-3.8-flash 503s under load and gemini-2.5-flash 404s on generateContent
# for this key, so 3.5 leads and 3.6 backs it up. Both verified serving.
PRIMARY_MODEL = "gemini-3.5-flash"
FALLBACK_MODEL = "gemini-3.6-flash"
BATCH_SIZE = 10
# The SDK retries internally as well, so keep our own attempts low or the two
# layers multiply into very long stalls when a model is throwing 503s.
MAX_RETRIES = 2
RETRY_BASE_SECONDS = 5
REQUEST_TIMEOUT_MS = 300_000
BODY_CHAR_LIMIT = 8000

COLUMNS = [
    "article_id",
    "date",
    "township",
    "event_type",
    "killed",
    "injured",
    "displaced",
    "scope",
    "notes",
    "outlet",
    "url",
]

EVENT_TYPES = {
    "airstrike",
    "artillery",
    "ground_clash",
    "arrest_detention",
    "displacement",
    "conscription",
    "aid_access",
    "other",
}

RULES = """1. Never skip an article. Every article produces at least one row.
2. If the article states a number anywhere — including the headline —
   record it. Strip the qualifier: "more than 5,000" -> 5000,
   "at least 20" -> 20. Put the original wording in notes.
3. Write UNCLEAR only when the article gives no figure at all for that
   column. "dozens" and "many" are UNCLEAR.
4. event_type is one of: airstrike, artillery, ground_clash,
   arrest_detention, displacement, conscription, aid_access, other.
5. scope is "incident" for a single dated event, or "period_summary"
   for a total across a month or a year. A summary still gets a row.
6. One article covering several townships gets one row per township."""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "rows": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "article_id": {"type": "STRING"},
                    "date": {"type": "STRING"},
                    "township": {"type": "STRING"},
                    "event_type": {"type": "STRING", "enum": sorted(EVENT_TYPES)},
                    "killed": {"type": "STRING"},
                    "injured": {"type": "STRING"},
                    "displaced": {"type": "STRING"},
                    "scope": {"type": "STRING", "enum": ["incident", "period_summary"]},
                    "notes": {"type": "STRING"},
                },
                "required": [
                    "article_id",
                    "date",
                    "township",
                    "event_type",
                    "killed",
                    "injured",
                    "displaced",
                    "scope",
                    "notes",
                ],
            },
        }
    },
    "required": ["rows"],
}


def load_articles(path=INPUT_CSV):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for i, row in enumerate(rows, start=1):
        row["article_id"] = str(i)
    return rows


def build_prompt(batch):
    articles = []
    for article in batch:
        articles.append(
            "\n".join(
                [
                    f"ARTICLE_ID: {article['article_id']}",
                    f"DATE: {article['date']}",
                    f"TITLE: {article['title']}",
                    f"BODY: {article['body'][:BODY_CHAR_LIMIT]}",
                ]
            )
        )

    return f"""You extract township-level conflict events from Myanmar news articles.

RULES:
{RULES}

Additional field guidance:
- article_id must copy the ARTICLE_ID of the article the row came from.
- date is the date of the event in YYYY-MM-DD form. If the article does not
  state an event date, use the article's DATE.
- township is the township or place name the event happened in. Use UNCLEAR
  only if no place is named at all.
- killed, injured and displaced are bare integers as strings ("5000"), or the
  word UNCLEAR.
- notes is a short quote or paraphrase of the original wording behind the
  figures, including qualifiers like "more than" or "at least".

Return JSON matching the schema. Every ARTICLE_ID below must appear in at
least one row.

ARTICLES:

{chr(10).join(articles)}"""


def fallback_row(article):
    return {
        "article_id": article["article_id"],
        "date": article["date"],
        "township": "UNCLEAR",
        "event_type": "other",
        "killed": "UNCLEAR",
        "injured": "UNCLEAR",
        "displaced": "UNCLEAR",
        "scope": "incident",
        "notes": "No row returned by model; placeholder to keep article represented.",
    }


def normalise_figure(value):
    """Bare integer as a string, or UNCLEAR.

    Rule 2 says a stated number is kept even when wrapped in a qualifier, so
    salvage the first integer if the model left "at least 3" intact. Wordy
    amounts like "dozens" have no digits and stay UNCLEAR under rule 3.
    """
    if value is None:
        return "UNCLEAR"
    text = str(value).strip().replace(",", "")
    match = re.search(r"\d+", text)
    if not match:
        return "UNCLEAR"
    return str(int(match.group()))


def rows_from_payload(payload, batch):
    """Merge model rows with article metadata, guaranteeing one row per article."""
    by_id = {article["article_id"]: article for article in batch}
    rows = []
    seen_ids = set()

    for row in payload.get("rows", []):
        article_id = str(row.get("article_id", "")).strip()
        article = by_id.get(article_id)
        if article is None:
            continue
        seen_ids.add(article_id)

        event_type = str(row.get("event_type", "")).strip()
        scope = str(row.get("scope", "")).strip()
        date = str(row.get("date", "")).strip()

        rows.append(
            {
                "article_id": article_id,
                "date": date or article["date"],
                "township": str(row.get("township", "")).strip() or "UNCLEAR",
                "event_type": event_type if event_type in EVENT_TYPES else "other",
                "killed": normalise_figure(row.get("killed")),
                "injured": normalise_figure(row.get("injured")),
                "displaced": normalise_figure(row.get("displaced")),
                "scope": scope if scope in {"incident", "period_summary"} else "incident",
                "notes": str(row.get("notes", "")).strip(),
                "outlet": article["outlet"],
                "url": article["url"],
            }
        )

    for article_id, article in by_id.items():
        if article_id not in seen_ids:
            row = fallback_row(article)
            row["outlet"] = article["outlet"]
            row["url"] = article["url"]
            rows.append(row)

    return rows


def call_model(client, model, prompt):
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
        ),
    )
    return json.loads(response.text)


def extract_batch(client, model, batch):
    """Return (payload, model).

    These flash models throw transient 503s under load, so each model gets
    several attempts with exponential backoff. A 404 means the model is not
    served at all, so move on immediately. If the primary never recovers, drop
    to the fallback rather than losing the run.
    """
    prompt = build_prompt(batch)
    candidates = [model]
    if FALLBACK_MODEL not in candidates:
        candidates.append(FALLBACK_MODEL)

    last_error = None
    for candidate in candidates:
        for attempt in range(MAX_RETRIES):
            try:
                return call_model(client, candidate, prompt), candidate
            except errors.ClientError as exc:
                last_error = exc
                if exc.code == 404:
                    print(f"  {candidate} returned 404, not served — moving on", flush=True)
                    break
                if exc.code == 429 and attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_BASE_SECONDS * 2 ** attempt)
                    continue
                raise
            except (errors.ServerError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_BASE_SECONDS * 2 ** attempt
                    print(f"  {candidate} unavailable, retrying in {delay}s", flush=True)
                    time.sleep(delay)
                    continue
                print(f"  {candidate} still failing after {MAX_RETRIES} attempts", flush=True)

    raise RuntimeError(f"all models failed for this batch: {last_error}")


def main():
    load_dotenv()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY is not set (put it in .env or export it)")

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=REQUEST_TIMEOUT_MS,
            # One HTTP attempt per call; retries and backoff are handled below.
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    articles = load_articles()
    model = PRIMARY_MODEL
    all_rows = []

    for start in range(0, len(articles), BATCH_SIZE):
        batch = articles[start : start + BATCH_SIZE]
        label = f"Batch {start // BATCH_SIZE + 1}: articles {batch[0]['article_id']}-{batch[-1]['article_id']}"
        print(label, flush=True)
        started = time.time()
        payload, model = extract_batch(client, model, batch)
        rows = rows_from_payload(payload, batch)
        all_rows.extend(rows)
        print(f"  {len(rows)} rows from {model} in {time.time() - started:.0f}s", flush=True)

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(all_rows)

    no_figure = sum(
        1
        for row in all_rows
        if row["killed"] == "UNCLEAR"
        and row["injured"] == "UNCLEAR"
        and row["displaced"] == "UNCLEAR"
    )
    print(f"Wrote {len(all_rows)} rows to {OUTPUT_CSV} using {model}")
    print(f"Rows carrying no figure at all: {no_figure}")


if __name__ == "__main__":
    main()
