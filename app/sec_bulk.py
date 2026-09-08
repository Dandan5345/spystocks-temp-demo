"""Local query store for the SEC's quarterly Form 3/4/5 data sets.

The public profile path must not download filings while a user is waiting.  This module
does the expensive work ahead of time: import a quarterly ``form345.zip`` into SQLite,
then reconstruct the same filing dictionaries consumed by ``summarize_filings``.
"""

from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
import tempfile
import threading
import urllib.request
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator


DATASET_ROOT = (
    "https://www.sec.gov/files/datastandardsinnovation/data/"
    "insider-transactions-data-sets"
)
LEGACY_DATASET_ROOT = (
    "https://www.sec.gov/files/structureddata/data/"
    "insider-transactions-data-sets"
)
DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "sec-ownership.sqlite3"
_TABLE_FILES = {
    "SUBMISSION.tsv": "submissions",
    "REPORTINGOWNER.tsv": "reporting_owners",
    "NONDERIV_TRANS.tsv": "non_derivative_transactions",
    "NONDERIV_HOLDING.tsv": "non_derivative_holdings",
    "DERIV_TRANS.tsv": "derivative_transactions",
    "DERIV_HOLDING.tsv": "derivative_holdings",
}


def quarter_url(quarter: str) -> str:
    normalized = quarter.lower()
    if len(normalized) != 6 or normalized[4] != "q" or not normalized[:4].isdigit() or normalized[5] not in "1234":
        raise ValueError("quarter must look like 2026q2")
    root = DATASET_ROOT if normalized >= "2026q2" else LEGACY_DATASET_ROOT
    return f"{root}/{normalized}_form345.zip"


def _date(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d-%b-%Y").date().isoformat()
    except ValueError:
        return value


def _float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


def _cik(value: str | None) -> str | None:
    value = (value or "").strip()
    return value.zfill(10) if value else None


def _rows(archive: zipfile.ZipFile, filename: str) -> Iterator[dict[str, str]]:
    with archive.open(filename) as raw:
        with io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="") as text:
            yield from csv.DictReader(text, delimiter="\t")


class BulkOwnershipStore:
    """Thread-safe SQLite ownership store optimized for person-CIK profile reads."""

    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._search_index_enabled = False

    def connect(self) -> sqlite3.Connection:
        if self._connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self.path, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._create_schema()
        return self._connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _create_schema(self) -> None:
        self._connection.executescript(  # type: ignore[union-attr]
            """
            CREATE TABLE IF NOT EXISTS import_runs (
              quarter TEXT PRIMARY KEY,
              source_url TEXT NOT NULL,
              imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              submission_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS submissions (
              accession TEXT PRIMARY KEY,
              quarter TEXT NOT NULL,
              filing_date TEXT,
              report_date TEXT,
              form TEXT,
              issuer_cik TEXT,
              issuer_name TEXT,
              issuer_ticker TEXT,
              remarks TEXT
            );
            CREATE TABLE IF NOT EXISTS reporting_owners (
              accession TEXT NOT NULL REFERENCES submissions(accession) ON DELETE CASCADE,
              owner_cik TEXT NOT NULL,
              owner_name TEXT,
              relationship TEXT,
              title TEXT,
              other_text TEXT,
              city TEXT,
              state TEXT,
              country TEXT,
              PRIMARY KEY (accession, owner_cik)
            );
            CREATE TABLE IF NOT EXISTS non_derivative_transactions (
              accession TEXT NOT NULL REFERENCES submissions(accession) ON DELETE CASCADE,
              row_key TEXT NOT NULL,
              security TEXT,
              transaction_date TEXT,
              code TEXT,
              acquired_disposed TEXT,
              shares REAL,
              price REAL,
              shares_after REAL,
              ownership TEXT,
              nature TEXT,
              PRIMARY KEY (accession, row_key)
            );
            CREATE TABLE IF NOT EXISTS non_derivative_holdings (
              accession TEXT NOT NULL REFERENCES submissions(accession) ON DELETE CASCADE,
              row_key TEXT NOT NULL,
              security TEXT,
              shares_after REAL,
              ownership TEXT,
              nature TEXT,
              PRIMARY KEY (accession, row_key)
            );
            CREATE TABLE IF NOT EXISTS derivative_transactions (
              accession TEXT NOT NULL REFERENCES submissions(accession) ON DELETE CASCADE,
              row_key TEXT NOT NULL,
              security TEXT,
              transaction_date TEXT,
              code TEXT,
              acquired_disposed TEXT,
              shares REAL,
              price REAL,
              shares_after REAL,
              underlying_security TEXT,
              underlying_shares REAL,
              exercise_price REAL,
              expiration_date TEXT,
              PRIMARY KEY (accession, row_key)
            );
            CREATE TABLE IF NOT EXISTS derivative_holdings (
              accession TEXT NOT NULL REFERENCES submissions(accession) ON DELETE CASCADE,
              row_key TEXT NOT NULL,
              security TEXT,
              shares_after REAL,
              underlying_security TEXT,
              underlying_shares REAL,
              exercise_price REAL,
              expiration_date TEXT,
              PRIMARY KEY (accession, row_key)
            );
            CREATE INDEX IF NOT EXISTS owner_profile_idx
              ON reporting_owners(owner_cik, accession);
            CREATE INDEX IF NOT EXISTS owner_name_idx
              ON reporting_owners(owner_name, owner_cik);
            CREATE INDEX IF NOT EXISTS submissions_profile_idx
              ON submissions(filing_date DESC, accession);
            CREATE INDEX IF NOT EXISTS issuer_profile_idx
              ON submissions(issuer_cik, filing_date DESC);
            """
        )
        self._ensure_search_index()
        self._connection.commit()  # type: ignore[union-attr]

    @staticmethod
    def _normalized_search_name(name: str | None) -> str:
        return " ".join(re.sub(r"[^A-Z0-9 ]+", " ", (name or "").upper()).split())

    def _ensure_search_index(self) -> None:
        """Create a compact FTS index of unique owners for type-ahead searches.

        Searching the filings table directly used to scan and group hundreds of
        thousands of rows for every keystroke.  The index contains one row per
        owner/name pair and supports token-prefix lookup without touching filings.
        """
        db = self._connection
        if db is None:
            return
        try:
            db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS owner_search USING fts5("
                "owner_cik UNINDEXED, owner_name UNINDEXED, normalized_name, "
                "tokenize='unicode61')"
            )
            indexed = db.execute("SELECT COUNT(*) FROM owner_search").fetchone()[0]
            owners = db.execute(
                "SELECT COUNT(*) FROM ("
                "SELECT 1 FROM reporting_owners GROUP BY owner_cik, owner_name)"
            ).fetchone()[0]
            if indexed != owners:
                self._rebuild_search_index(db)
            self._search_index_enabled = True
        except sqlite3.OperationalError:
            # Some system SQLite builds omit FTS5. Search still works through the
            # smaller DISTINCT fallback below, just without the prefix index.
            self._search_index_enabled = False

    def _rebuild_search_index(self, db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM owner_search")
        rows = db.execute(
            "SELECT owner_cik, owner_name FROM reporting_owners "
            "WHERE owner_name IS NOT NULL GROUP BY owner_cik, owner_name"
        ).fetchall()
        db.executemany(
            "INSERT INTO owner_search(owner_cik, owner_name, normalized_name) VALUES (?, ?, ?)",
            ((row[0], row[1], self._normalized_search_name(row[1])) for row in rows),
        )

    @contextmanager
    def _transaction(self):
        connection = self.connect()
        with self._lock:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def import_zip(self, zip_path: Path | str, quarter: str, *, source_url: str | None = None) -> dict[str, int]:
        """Replace one quarter atomically; safe to rerun after SEC republishes a file."""
        quarter_url(quarter)  # validation
        counts = {table: 0 for table in _TABLE_FILES.values()}
        with zipfile.ZipFile(zip_path) as archive:
            missing = set(_TABLE_FILES) - set(archive.namelist())
            if missing:
                raise ValueError(f"invalid form345 archive; missing: {', '.join(sorted(missing))}")
            with self._transaction() as db:
                db.execute("DELETE FROM submissions WHERE quarter = ?", (quarter.lower(),))
                db.execute("DELETE FROM import_runs WHERE quarter = ?", (quarter.lower(),))

                counts["submissions"] = self._insert_submissions(db, _rows(archive, "SUBMISSION.tsv"), quarter.lower())
                counts["reporting_owners"] = self._insert_owners(db, _rows(archive, "REPORTINGOWNER.tsv"))
                counts["non_derivative_transactions"] = self._insert_non_deriv(db, _rows(archive, "NONDERIV_TRANS.tsv"))
                counts["non_derivative_holdings"] = self._insert_non_deriv_holdings(db, _rows(archive, "NONDERIV_HOLDING.tsv"))
                counts["derivative_transactions"] = self._insert_deriv(db, _rows(archive, "DERIV_TRANS.tsv"))
                counts["derivative_holdings"] = self._insert_deriv_holdings(db, _rows(archive, "DERIV_HOLDING.tsv"))
                db.execute(
                    "INSERT INTO import_runs(quarter, source_url, submission_count) VALUES (?, ?, ?)",
                    (quarter.lower(), source_url or str(zip_path), counts["submissions"]),
                )
                if self._search_index_enabled:
                    self._rebuild_search_index(db)
        return counts

    @staticmethod
    def _many(db: sqlite3.Connection, sql: str, values: Iterable[tuple[Any, ...]], batch_size: int = 5000) -> int:
        batch: list[tuple[Any, ...]] = []
        count = 0
        for value in values:
            batch.append(value)
            if len(batch) >= batch_size:
                db.executemany(sql, batch)
                count += len(batch)
                batch.clear()
        if batch:
            db.executemany(sql, batch)
            count += len(batch)
        return count

    def _insert_submissions(self, db, rows, quarter):
        return self._many(db, "INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?,?)", (
            (r["ACCESSION_NUMBER"], quarter, _date(r.get("FILING_DATE")), _date(r.get("PERIOD_OF_REPORT")),
             r.get("DOCUMENT_TYPE"), _cik(r.get("ISSUERCIK")), r.get("ISSUERNAME"),
             r.get("ISSUERTRADINGSYMBOL"), r.get("REMARKS")) for r in rows
        ))

    def _insert_owners(self, db, rows):
        return self._many(db, "INSERT OR REPLACE INTO reporting_owners VALUES (?,?,?,?,?,?,?,?,?)", (
            (r["ACCESSION_NUMBER"], _cik(r.get("RPTOWNERCIK")), r.get("RPTOWNERNAME"),
             r.get("RPTOWNER_RELATIONSHIP"), r.get("RPTOWNER_TITLE"), r.get("RPTOWNER_TXT"),
             r.get("RPTOWNER_CITY"), r.get("RPTOWNER_STATE"), r.get("RPTOWNER_STATE_DESC")) for r in rows
            if _cik(r.get("RPTOWNERCIK"))
        ))

    def _insert_non_deriv(self, db, rows):
        return self._many(db, "INSERT OR REPLACE INTO non_derivative_transactions VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            (r["ACCESSION_NUMBER"], r["NONDERIV_TRANS_SK"], r.get("SECURITY_TITLE"), _date(r.get("TRANS_DATE")),
             r.get("TRANS_CODE"), r.get("TRANS_ACQUIRED_DISP_CD"), _float(r.get("TRANS_SHARES")),
             _float(r.get("TRANS_PRICEPERSHARE")), _float(r.get("SHRS_OWND_FOLWNG_TRANS")),
             r.get("DIRECT_INDIRECT_OWNERSHIP"), r.get("NATURE_OF_OWNERSHIP")) for r in rows
        ))

    def _insert_non_deriv_holdings(self, db, rows):
        return self._many(db, "INSERT OR REPLACE INTO non_derivative_holdings VALUES (?,?,?,?,?,?)", (
            (r["ACCESSION_NUMBER"], r["NONDERIV_HOLDING_SK"], r.get("SECURITY_TITLE"),
             _float(r.get("SHRS_OWND_FOLWNG_TRANS")), r.get("DIRECT_INDIRECT_OWNERSHIP"),
             r.get("NATURE_OF_OWNERSHIP")) for r in rows
        ))

    def _insert_deriv(self, db, rows):
        return self._many(db, "INSERT OR REPLACE INTO derivative_transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            (r["ACCESSION_NUMBER"], r["DERIV_TRANS_SK"], r.get("SECURITY_TITLE"), _date(r.get("TRANS_DATE")),
             r.get("TRANS_CODE"), r.get("TRANS_ACQUIRED_DISP_CD"), _float(r.get("TRANS_SHARES")),
             _float(r.get("TRANS_PRICEPERSHARE")), _float(r.get("SHRS_OWND_FOLWNG_TRANS")),
             r.get("UNDLYNG_SEC_TITLE"), _float(r.get("UNDLYNG_SEC_SHARES")),
             _float(r.get("CONV_EXERCISE_PRICE")), _date(r.get("EXPIRATION_DATE"))) for r in rows
        ))

    def _insert_deriv_holdings(self, db, rows):
        return self._many(db, "INSERT OR REPLACE INTO derivative_holdings VALUES (?,?,?,?,?,?,?,?)", (
            (r["ACCESSION_NUMBER"], r["DERIV_HOLDING_SK"], r.get("SECURITY_TITLE"),
             _float(r.get("SHRS_OWND_FOLWNG_TRANS")), r.get("UNDLYNG_SEC_TITLE"),
             _float(r.get("UNDLYNG_SEC_SHARES")), _float(r.get("CONV_EXERCISE_PRICE")),
             _date(r.get("EXPIRATION_DATE"))) for r in rows
        ))

    def has_owner(self, cik: str) -> bool:
        with self._lock:
            row = self.connect().execute(
                "SELECT 1 FROM reporting_owners WHERE owner_cik = ? LIMIT 1", (_cik(cik),)
            ).fetchone()
        return row is not None

    def search_owner_candidates(self, tokens: list[str], limit: int = 1500) -> list[dict[str, Any]]:
        """Return a small active-insider candidate set for application-side ranking."""
        cleaned = [
            normalized
            for token in tokens
            if len(normalized := self._normalized_search_name(token)) >= 2
        ][:4]
        if not cleaned:
            return []
        row_limit = max(1, min(limit, 5000))
        with self._lock:
            db = self.connect()
            if self._search_index_enabled:
                # Exact token combinations are most useful, followed by each token
                # independently for public/legal name differences (Jensen/Jen Hsun).
                if len(cleaned) > 1:
                    exact_rows = db.execute(
                        "SELECT owner_cik AS cik, owner_name AS name FROM owner_search "
                        "WHERE normalized_name MATCH ? LIMIT ?",
                        (" AND ".join(f'"{token}"*' for token in cleaned), row_limit),
                    ).fetchall()
                    if exact_rows:
                        return [dict(row) for row in exact_rows]

                expressions = [f'"{token}"*' for token in sorted(cleaned, key=len, reverse=True)]
                found: dict[tuple[str, str], dict[str, Any]] = {}
                for expression in expressions:
                    rows = db.execute(
                        "SELECT owner_cik AS cik, owner_name AS name FROM owner_search "
                        "WHERE normalized_name MATCH ? LIMIT ?",
                        (expression, row_limit),
                    ).fetchall()
                    for row in rows:
                        item = dict(row)
                        found.setdefault((item["cik"], item["name"]), item)
                return list(found.values())

            # Portable fallback: avoid the submissions join, MAX and date sort. None
            # of those values participate in name ranking and they made short queries
            # especially expensive.
            where = " OR ".join("UPPER(owner_name) LIKE ?" for _ in cleaned)
            rows = db.execute(
                f"SELECT DISTINCT owner_cik AS cik, owner_name AS name "
                f"FROM reporting_owners WHERE {where} LIMIT ?",
                [f"%{token}%" for token in cleaned] + [row_limit],
            ).fetchall()
            return [dict(row) for row in rows]

    def top_transactions(self, cik: str, limit: int = 20) -> list[dict[str, Any]]:
        """Fetch only the financially significant activity for the initial paint.

        This is deliberately a single local query.  Full filing/holding history can be
        requested later without delaying the biography/header portion of a profile.
        """
        owner_cik = _cik(cik)
        sql = """
            SELECT * FROM (
              SELECT s.accession, s.filing_date, s.issuer_cik, s.issuer_name,
                     s.issuer_ticker, t.transaction_date, t.security, t.code,
                     t.acquired_disposed, t.shares, t.price, t.shares_after,
                     t.shares * t.price AS transaction_value, 0 AS derivative
              FROM reporting_owners o
              JOIN submissions s ON s.accession = o.accession
              JOIN non_derivative_transactions t ON t.accession = s.accession
              WHERE o.owner_cik = ? AND t.shares IS NOT NULL AND t.price IS NOT NULL
              UNION ALL
              SELECT s.accession, s.filing_date, s.issuer_cik, s.issuer_name,
                     s.issuer_ticker, t.transaction_date, t.security, t.code,
                     t.acquired_disposed, t.shares, t.price, t.shares_after,
                     t.shares * t.price AS transaction_value, 1 AS derivative
              FROM reporting_owners o
              JOIN submissions s ON s.accession = o.accession
              JOIN derivative_transactions t ON t.accession = s.accession
              WHERE o.owner_cik = ? AND t.shares IS NOT NULL AND t.price IS NOT NULL
            ) ORDER BY ABS(transaction_value) DESC, transaction_date DESC LIMIT ?
        """
        with self._lock:
            rows = self.connect().execute(sql, (owner_cik, owner_cik, max(1, min(limit, 100)))).fetchall()
        return [
            {
                "company": row["issuer_name"], "ticker": row["issuer_ticker"],
                "issuer_cik": row["issuer_cik"], "security": row["security"],
                "date": row["transaction_date"], "filing_date": row["filing_date"],
                "code": row["code"], "acquired_disposed": row["acquired_disposed"],
                "shares": row["shares"], "price": row["price"],
                "value": round(row["transaction_value"], 2), "shares_after": row["shares_after"],
                "accession": row["accession"], "derivative": bool(row["derivative"]),
                "sec_url": f"https://www.sec.gov/Archives/edgar/data/{int(row['accession'].split('-', 1)[0])}/"
                           f"{row['accession'].replace('-', '')}/",
            }
            for row in rows
        ]

    def profile_header(self, cik: str) -> dict[str, Any] | None:
        """Return identity, current role and company timeline without loading transactions."""
        owner_cik = _cik(cik)
        with self._lock:
            db = self.connect()
            latest = db.execute(
                """SELECT s.*, o.* FROM reporting_owners o
                   JOIN submissions s ON s.accession = o.accession
                   WHERE o.owner_cik = ?
                   ORDER BY s.filing_date DESC, s.accession DESC LIMIT 1""",
                (owner_cik,),
            ).fetchone()
            if latest is None:
                return None
            companies = db.execute(
                """SELECT s.issuer_cik AS cik, s.issuer_name AS name, s.issuer_ticker AS ticker,
                          MIN(s.filing_date) AS first_seen, MAX(s.filing_date) AS last_seen,
                          COUNT(*) AS filings
                   FROM reporting_owners o JOIN submissions s ON s.accession = o.accession
                   WHERE o.owner_cik = ? GROUP BY s.issuer_cik, s.issuer_name, s.issuer_ticker
                   ORDER BY last_seen DESC""",
                (owner_cik,),
            ).fetchall()
            roles = db.execute(
                """SELECT s.issuer_name AS company, s.issuer_ticker AS ticker,
                          o.title, o.relationship,
                          MIN(s.filing_date) AS first_seen, MAX(s.filing_date) AS last_seen
                   FROM reporting_owners o JOIN submissions s ON s.accession = o.accession
                   WHERE o.owner_cik = ?
                   GROUP BY s.issuer_cik, s.issuer_name, s.issuer_ticker, o.title, o.relationship
                   ORDER BY last_seen DESC""",
                (owner_cik,),
            ).fetchall()
            filing_count = db.execute(
                "SELECT COUNT(*) FROM reporting_owners WHERE owner_cik = ?", (owner_cik,)
            ).fetchone()[0]
            transaction_stats = db.execute(
                """SELECT COUNT(*) AS transaction_count,
                          COALESCE(SUM(CASE WHEN acquired_disposed = 'A' THEN value ELSE 0 END), 0) AS purchased_value,
                          COALESCE(SUM(CASE WHEN acquired_disposed = 'D' THEN value ELSE 0 END), 0) AS disposed_value
                   FROM (
                     SELECT t.acquired_disposed, t.shares * t.price AS value
                     FROM reporting_owners o
                     JOIN non_derivative_transactions t ON t.accession = o.accession
                     WHERE o.owner_cik = ?
                     UNION ALL
                     SELECT t.acquired_disposed, t.shares * t.price AS value
                     FROM reporting_owners o
                     JOIN derivative_transactions t ON t.accession = o.accession
                     WHERE o.owner_cik = ?
                   )""",
                (owner_cik, owner_cik),
            ).fetchone()
            dataset = db.execute(
                "SELECT MIN(quarter) AS first_quarter, MAX(quarter) AS last_quarter, COUNT(*) AS quarters FROM import_runs"
            ).fetchone()
        normalized_roles = []
        for role in roles:
            label = role["title"] or role["relationship"] or "Insider"
            normalized_roles.append({
                "company": role["company"], "ticker": role["ticker"], "role": label,
                "first_seen": role["first_seen"], "last_seen": role["last_seen"],
            })
        return {
            "person": {
                "name": latest["owner_name"], "cik": owner_cik,
                "latest_location": {"city": latest["city"], "state": latest["state"], "country": latest["country"]},
            },
            "current_role": {"title": latest["title"], "relationship": latest["relationship"],
                             "company": latest["issuer_name"], "ticker": latest["issuer_ticker"]},
            "companies": [dict(row) for row in companies],
            "roles": normalized_roles,
            "stats": {
                "filings_parsed": filing_count,
                "companies": len(companies),
                "transactions": transaction_stats["transaction_count"],
                "purchased_value": round(transaction_stats["purchased_value"], 2),
                "disposed_value": round(transaction_stats["disposed_value"], 2),
            },
            "dataset": dict(dataset),
        }

    def profile_filings(self, cik: str, limit: int = 120) -> list[dict[str, Any]]:
        """Return filing details in the shape expected by sec_client.summarize_filings."""
        owner_cik = _cik(cik)
        with self._lock:
            db = self.connect()
            filings = db.execute(
                """SELECT s.*, o.* FROM reporting_owners o
                   JOIN submissions s ON s.accession = o.accession
                   WHERE o.owner_cik = ?
                   ORDER BY s.filing_date DESC, s.accession DESC LIMIT ?""",
                (owner_cik, max(1, min(limit, 500))),
            ).fetchall()
            return [self._filing(db, row, owner_cik or "") for row in filings]

    @staticmethod
    def _filing(db: sqlite3.Connection, row: sqlite3.Row, owner_cik: str) -> dict[str, Any]:
        accession = row["accession"]
        relation = (row["relationship"] or "").lower()
        archive_base = (
            f"https://www.sec.gov/Archives/edgar/data/{int(accession.split('-', 1)[0])}/"
            f"{accession.replace('-', '')}/"
        )
        result: dict[str, Any] = {
            "accession": accession, "form": row["form"], "filing_date": row["filing_date"],
            "report_date": row["report_date"],
            "issuer": {"cik": row["issuer_cik"], "name": row["issuer_name"], "ticker": row["issuer_ticker"]},
            "owner": {"cik": owner_cik, "name": row["owner_name"],
                      "is_director": "director" in relation, "is_officer": "officer" in relation,
                      "is_ten_percent_owner": "10%" in relation or "ten percent" in relation,
                      "is_other": "other" in relation, "title": row["title"], "other_text": row["other_text"],
                      "location": {"city": row["city"], "state": row["state"], "country": row["country"]}},
            "remarks": row["remarks"], "non_derivative": [], "derivative": [],
            "sec_url": archive_base,
        }
        for tx in db.execute("SELECT * FROM non_derivative_transactions WHERE accession = ?", (accession,)):
            shares, price = tx["shares"], tx["price"]
            result["non_derivative"].append({
                "security": tx["security"], "date": tx["transaction_date"], "code": tx["code"],
                "acquired_disposed": tx["acquired_disposed"], "shares": shares, "price": price,
                "value": round(shares * price, 2) if shares is not None and price is not None else None,
                "shares_after": tx["shares_after"], "ownership": tx["ownership"], "nature": tx["nature"],
            })
        for holding in db.execute("SELECT * FROM non_derivative_holdings WHERE accession = ?", (accession,)):
            result["non_derivative"].append({
                "security": holding["security"], "date": row["report_date"], "code": "HOLDING",
                "acquired_disposed": None, "shares": None, "price": None, "value": None,
                "shares_after": holding["shares_after"], "ownership": holding["ownership"],
                "nature": holding["nature"],
            })
        for tx in db.execute("SELECT * FROM derivative_transactions WHERE accession = ?", (accession,)):
            shares, price = tx["shares"], tx["price"]
            result["derivative"].append({
                "security": tx["security"], "date": tx["transaction_date"], "code": tx["code"],
                "acquired_disposed": tx["acquired_disposed"], "shares": shares, "price": price,
                "value": round(shares * price, 2) if shares is not None and price is not None else None,
                "underlying_security": tx["underlying_security"], "underlying_shares": tx["underlying_shares"],
                "exercise_price": tx["exercise_price"], "expiration_date": tx["expiration_date"],
                "shares_after": tx["shares_after"],
            })
        return result


def download_quarter(quarter: str, destination: Path | str, *, user_agent: str | None = None) -> Path:
    """Download one SEC archive atomically so interrupted updates leave no partial ZIP."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        quarter_url(quarter),
        headers={"User-Agent": user_agent or os.getenv("SEC_USER_AGENT", "Information Check System contact@example.com")},
    )
    fd, temporary = tempfile.mkstemp(prefix=f".{quarter}.", suffix=".zip", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as target, urllib.request.urlopen(request, timeout=60) as response:
            while chunk := response.read(1024 * 1024):
                target.write(chunk)
        Path(temporary).replace(destination)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise
    return destination


ownership_store = BulkOwnershipStore()
