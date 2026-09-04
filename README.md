# BNI Scraper

Collects news from [Burma News International](https://www.bnionline.net/en/news)
and turns each story into structured township-level event rows.

## The two scripts

### `scrape.py`

Walks the news listing at `/en/news?page=N` for the first 3 pages, then opens
each story to collect its full text. Writes `articles.csv`:

| column | meaning |
| --- | --- |
| `date` | publication date, `YYYY-MM-DD` |
| `outlet` | the BNI member agency that filed the story |
| `title` | headline, from the `og:title` meta tag |
| `words` | word count of `body` |
| `url` | article URL |
| `body` | article text, one paragraph per line |

BNI runs Drupal 7 and leaves `<p>` tags unclosed, so the **lxml parser is
required** — `html.parser` nests the unclosed tags instead of closing them and
every paragraph comes out twice.

There is no usable feed to scrape instead: `sitemap.xml` lists only section
pages, and `rss.xml` has been frozen since November 2023.

### `extract.py`

Reads `articles.csv` and sends the stories to the Gemini API in batches of 10,
asking for one row per township-level event. Writes `extracted.csv`:

| column | meaning |
| --- | --- |
| `article_id` | 1-based row number in `articles.csv` |
| `date` | date of the event, falling back to the article date |
| `township` | where it happened, or `UNCLEAR` |
| `event_type` | `airstrike`, `artillery`, `ground_clash`, `arrest_detention`, `displacement`, `conscription`, `aid_access`, `other` |
| `killed` / `injured` / `displaced` | bare integer, or `UNCLEAR` |
| `scope` | `incident` for a single dated event, `period_summary` for a total |
| `notes` | the original wording behind the figures |
| `outlet` / `url` | joined from `articles.csv`, not asked of the model |

Every article is guaranteed at least one row: if the model skips one, the
merge step inserts a placeholder rather than dropping the article.

**Model choice.** `gemini-3.5-flash` leads, with `gemini-3.6-flash` as fallback.
Both were verified serving this key. `gemini-3.8-flash` returns 503 under real
load and `gemini-2.5-flash` returns 404 on `generateContent`, so neither is used.

## Running it

```bash
pip install -r requirements.txt
echo "GEMINI_API_KEY=your-key-here" > .env
python3 scrape.py
python3 extract.py
```

`.env` is gitignored. In CI the key comes from the `GEMINI_API_KEY` repository
secret instead.

## Automation

`.github/workflows/scrape.yml` runs both scripts daily at 06:00 Myanmar time
and commits the two CSVs back when they change. It can also be triggered by
hand from the Actions tab.

## Caveats

`extracted.csv` is model output and is not deduplicated — several articles
recapping the same incident each produce their own rows. Spot-check figures
against `body` in `articles.csv` before relying on them.
