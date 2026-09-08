# מערכת בדיקת מידע

A working explorer for **corporate insiders** from SEC EDGAR, **current or former members of Congress** from Congress.gov, and current presidential-administration officials from WhiteHouse.gov.

## What it does

- Searches the SEC's official cumulative **CIK lookup** (includes individuals and companies).
- Resolves a reporting owner's CIK from a human name.
- Loads the filer's complete submissions history from `data.sec.gov` (including archival submission shards when present).
- Filters Forms **3 / 4 / 5** and amendments.
- Fetches and parses the actual ownership XML for each filing.
- Extracts:
  - reporting-owner name + SEC CIK
  - director/officer/10% owner relationship
  - officer title
  - issuer/company + ticker
  - non-derivative transactions and holdings
  - derivative transactions
  - shares, price, transaction value when disclosed
  - shares owned after transaction
  - filing/report dates and exact SEC source link
- Builds a role/company timeline from the person's filings.
- Serves an instant local overview from the SEC's quarterly flattened Form 3/4/5 data,
  including roles, connected issuers and the largest disclosed transactions.
- Loads a conservatively identity-matched Wikipedia summary and portrait separately,
  with a visible source link; a missing/ambiguous match is never guessed.
- Best-effort **DEF 14A enrichment**: searches recent connected-company proxy statements for rows/blocks that explicitly mention the person, surfacing company-reported biography/role/age/experience context with the exact filing source.
- Best-effort **SEC-only portrait discovery**: checks recent connected-company proxy filings and only uses an image when the person's name is explicitly associated with that image in filing HTML context. Otherwise it intentionally shows initials instead of guessing.
- Responsive editorial interface with System, Light and Dark appearance modes.

## Politicians

Switch to **POLITICIANS** to search the locally cached Congress.gov member index by name. Search is case- and punctuation-tolerant, handles both `First Last` and `Last, First`, ranks exact and prefix matches first, and keeps ambiguous matches for the user to choose.

Politician profiles include:

- Congress.gov portrait with attribution, or an initials fallback when no portrait is supplied
- current/former status, chamber, party, state and district
- complete congressional term timeline and party history returned by Congress.gov
- birth year and official website when available
- true sponsored and cosponsored legislation totals
- lazily loaded recent legislation with pagination and official source links
- shareable `/politician/{bioguideId}` URLs
- normalized PTR transaction history from the official House Clerk or Senate eFD source selected from the member's Congress.gov chamber
- purchase, sale and spouse summaries, disclosed range totals, explicit tickers only, and a direct official filing link on every transaction

Search results also include officials with dedicated profiles in the current White House Administration directory—such as the President, Vice President and First/Second Lady. Their `/politician/whitehouse/{slug}` profiles show the official role, portrait, biography, update date, WhiteHouse.gov provenance, and publicly documented spouse/children relationships. Family-only profiles are clearly labelled and are never presented as government officials. WhiteHouse.gov does not expose a public people API, so the server conservatively parses its official Administration profile links and keeps persistent stale-while-revalidate snapshots.

The server uses these official Congress.gov API v3 endpoints:

- `GET /member` (all pages, 250 records per request)
- `GET /member/{bioguideId}`
- `GET /member/{bioguideId}/sponsored-legislation`
- `GET /member/{bioguideId}/cosponsored-legislation`

The normalized member index is cached at `data/congress-member-index.json`. Even when stale, it serves search immediately and refreshes in the background. Profile overview snapshots are persistent, so repeat and prepared-demo views do not wait for Congress.gov. Browser keystrokes only query the local application API; they never call Congress.gov directly.

Financial disclosures are fetched only by the backend. House profiles use the Clerk's official yearly disclosure index and PTR PDFs; scanned PDFs are read locally with macOS Vision OCR when no PDF text layer exists. Senate profiles establish the eFD public-search session, accept its official disclosure-use agreement, and read official PTR pages. Both adapters are throttled and cached. If Senate eFD's edge protection rejects the server, the profile reports the official source as unavailable rather than substituting third-party data. The normalized response reserves `annualData.assets`, `liabilities`, `positions`, and `income` for future annual-report parsing.

## Institutions

Switch to **INSTITUTIONS** to search investment managers that filed Form 13F-HR with the SEC during the latest eight quarters. The server builds a local name/CIK index from the SEC's official quarterly EDGAR `master.idx` files and caches it for 24 hours, so typing never triggers an SEC request.

An institution profile is served from a persistent snapshot when available. A missing snapshot first renders local manager identity immediately, then fills the detailed SEC data in the background. The builder reads the filer's `data.sec.gov/submissions/CIK##########.json`, selects the latest authoritative filing for each reporting period (including later 13F-HR amendments), and parses the latest two SEC Information Table XML documents. It provides:

- business or mailing address when reported
- recent SEC filings and up to 12 quarterly 13F filings
- total reported portfolio value and current holding count
- every latest holding through a paginated local API
- issuer, CUSIP, shares/principal amount, value, option designation and quarter
- NEW POSITION, INCREASED, REDUCED, EXITED and UNCHANGED classifications
- Top 10 holdings, biggest additions and biggest reductions
- direct links to the exact SEC filing and information table

Form 13F does not contain ticker symbols. The server only adds a ticker when the normalized issuer name has one unambiguous exact match in the SEC's official `company_tickers.json`; otherwise it displays the issuer name and CUSIP without guessing.

## Run

```bash
cd information-check-system
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export SEC_USER_AGENT="Information Check System YOUR_REAL_EMAIL@example.com"
export CONGRESS_API_KEY="YOUR_CONGRESS_GOV_API_KEY"
python run.py
```

Open: `http://127.0.0.1:8000`

### Build the fast insider database

Run this ahead of serving traffic (and again when the SEC publishes a new quarter):

```bash
source .venv/bin/activate
python scripts/sec_ingest.py 2024q3 2024q4 2025q1 2025q2 2025q3 2025q4 2026q1 2026q2
```

The importer downloads the official quarterly archives, replaces each quarter
atomically, and writes `data/sec-ownership.sqlite3`. With that file present, insider
search and the initial profile do no outbound SEC requests. On a measured eight-quarter
database, common searches took roughly 15–45 ms and the local profile query took roughly
1–6 ms. If the database is absent or an older person is not present in it, the app keeps
the live EDGAR path as a slower fallback.

Copy `.env.example` to `.env` for local development, or configure the same values in your deployment environment. `run.py` loads the local `.env`; the file is ignored by Git. `CONGRESS_API_KEY` is server-only, is sent to Congress.gov in an HTTP header, and must never use a `NEXT_PUBLIC_` or `VITE_` prefix.

### Prepare the approval demo

After configuring `.env`, warm the representative profiles used in a review session:

```bash
source .venv/bin/activate
python scripts/prepare_demo.py
```

This prepares popular institutions, Congress members, current White House officials and the public biography cards used by family profiles. It is safe to rerun and never prints API keys.

## Important SEC requirement

The SEC asks automated clients to identify themselves and limits automated access to **10 requests/second**. This prototype deliberately throttles itself below that limit.

## Coverage / reality check

- The SEC's dedicated flattened Insider Transactions Data Sets currently cover **January 2006 through June 2026**. The local database contains only the quarters you import; the UI reports that coverage explicitly. EDGAR filings after the latest imported quarter require a scheduled delta/backfill job.
- SEC ownership filings are a source of truth for **reported ownership activity**, not a complete biography. A person's education, age, full employment history or a professional headshot often simply do not exist in their own Forms 3/4/5.
- Portraits are intentionally conservative. SEC corporate proxy filings may contain executive/director photos, but not consistently and not always with machine-readable labels.

## Production upgrades

1. **Daily EDGAR delta** — ingest new Form 3/4/5 filings between quarterly SEC data-set releases.
2. **Scheduled profile enrichment** — refresh Wikipedia/proxy biography snapshots ahead of demand for consistently sub-second cold views.
3. **PostgreSQL/Redis deployment store** — use a shared database and stale-while-revalidate snapshots when running multiple workers.
4. **DEF 14A enrichment** — parse issuer proxy statements for executive bios, ages, compensation and longer career history.
5. **Entity resolution** — merge name variants and duplicate/changed CIK associations with confidence scores.
6. **Photo resolver** — SEC proxy image first; then an explicitly licensed fallback source if SEC-only coverage is too sparse.
7. **Pagination** — the API already accepts up to `max_filings=500`; production should page transactions and cache deeper history.

## Main endpoints

- `GET /api/search?q=Jensen%20Huang`
- `GET /api/profile/{cik}?max_filings=120&include_photo=true`
- `GET /api/politicians/search?q=Nancy%20Pelosi`
- `GET /api/politicians/{bioguideId}/overview`
- `GET /api/politicians/{bioguideId}`
- `GET /api/politicians/whitehouse/{slug}`
- `GET /api/politicians/{bioguideId}/legislation?kind=sponsored&offset=0&limit=12`
- `GET /api/institutions/search?q=Berkshire%20Hathaway`
- `GET /api/institutions/{cik}/overview`
- `GET /api/institutions/{cik}`
- `GET /api/institutions/{cik}/holdings?offset=100&limit=100`

## Tests

```bash
source .venv/bin/activate
python -m pytest
```

The suite covers Congress.gov parsing, pagination, member normalization, exact/partial/punctuation-tolerant name search, ambiguous matches, missing images, missing keys, API errors, secret isolation, and regression checks for the existing Insider endpoints.

## Data provenance

Every transaction returned by the backend contains the direct SEC filing URL used to derive it.
