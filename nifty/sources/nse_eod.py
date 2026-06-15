#!/usr/bin/env python3
"""
NSE EOD downloader + SQLite archive.

Official publication timing (IST, trading days):
  ~16:00-17:30  FII/DII provisional cash-market data
  ~18:00-18:30  CM bhavcopy, delivery report, market activity
  ~18:30-19:30  F&O bhavcopy, participant OI/vol reports

Use nse_eod_scheduler.py for timed retries after close.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

import pandas as pd
import requests


from nifty.paths import PROJECT_ROOT as BASE_DIR
RAW_DIR = BASE_DIR / "data" / "nse_eod" / "raw"
DB_PATH = BASE_DIR / "data" / "nse_eod" / "nse_eod.sqlite"
NSE_HOME = "https://www.nseindia.com"
FII_DII_URL = f"{NSE_HOME}/api/fiidiiTradeReact"
ALL_INDICES_URL = f"{NSE_HOME}/api/allIndices"

# IST publication windows used by scheduler
PUBLISH_WINDOWS = {
    "fii_dii": "16:00-17:30 IST provisional cash FII/DII",
    "cm_reports": "18:00-18:30 IST CM bhavcopy / delivery / market activity",
    "fo_reports": "18:30-19:30 IST F&O bhavcopy / participant OI & volume",
}


@dataclass
class DownloadTarget:
    name: str
    urls: List[str]
    output_name: str
    table_name: Optional[str] = None
    referer: str = NSE_HOME
    kind: str = "file"  # file | json_api


def nse_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
            "Referer": NSE_HOME,
        }
    )
    session.get(NSE_HOME, timeout=15)
    session.get(f"{NSE_HOME}/all-reports-equities", timeout=15)
    session.get(f"{NSE_HOME}/all-reports-derivatives", timeout=15)
    session.get(f"{NSE_HOME}/reports/fii-dii", timeout=15)
    return session


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS download_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                ok_count INTEGER NOT NULL,
                total_count INTEGER NOT NULL,
                manifest_path TEXT,
                notes TEXT
            )
            """
        )
        conn.commit()


def archive_urls(*paths: str) -> List[str]:
    bases = ("https://nsearchives.nseindia.com", "https://archives.nseindia.com")
    urls: List[str] = []
    for path in paths:
        clean = path.lstrip("/")
        for base in bases:
            urls.append(f"{base}/{clean}")
    return urls


def is_valid_download(response: requests.Response) -> bool:
    if response.status_code != 200 or not response.content:
        return False
    content_type = response.headers.get("Content-Type", "").lower()
    if "html" in content_type or "json" in content_type:
        return False
    head = response.content[:256].lstrip()
    if head.startswith(b"<!") or head.startswith(b"{"):
        return False
    if b'"error"' in head[:120] and head.startswith(b"{"):
        return False
    return True


def date_parts(day: date) -> Dict[str, str]:
    return {
        "dd": day.strftime("%d"),
        "mon": day.strftime("%b").upper(),
        "mon_title": day.strftime("%b").title(),
        "yyyy": day.strftime("%Y"),
        "yyyymmdd": day.strftime("%Y%m%d"),
        "ddmmyyyy": day.strftime("%d%m%Y"),
        "dd-mon-yyyy": day.strftime("%d-%b-%Y"),
    }


def daily_report_url(
    name: str,
    day: date,
    category: str = "derivatives",
    report_type: str = "derivatives",
    section: Optional[str] = None,
) -> str:
    archive = [{"name": name, "type": "daily-reports", "category": category}]
    if section:
        archive[0]["section"] = section
    archives = quote(json.dumps(archive, separators=(",", ":")))
    report_date = quote(day.strftime("%d-%b-%Y"))
    return f"https://www.nseindia.com/api/reports?archives={archives}&date={report_date}&type={report_type}&mode=single"


def cm_daily_report_url(name: str, day: date) -> str:
    return daily_report_url(name, day, category="capital-market", report_type="equities", section="equities")


def fo_daily_report_url(name: str, day: date) -> str:
    return daily_report_url(name, day, category="derivatives", report_type="derivatives")


DERIVATIVES_REFERER = f"{NSE_HOME}/all-reports-derivatives"
EQUITIES_REFERER = f"{NSE_HOME}/all-reports-equities"
FII_REFERER = f"{NSE_HOME}/reports/fii-dii"


def build_targets(day: date) -> List[DownloadTarget]:
    p = date_parts(day)
    ddmmyyyy = p["ddmmyyyy"]
    yyyymmdd = p["yyyymmdd"]
    fo_udiff_name = f"BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"
    cm_udiff_name = f"BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip"

    return [
        DownloadTarget(
            name="fii_dii",
            urls=[FII_DII_URL],
            output_name=f"fii_dii_{ddmmyyyy}.json",
            table_name="fii_dii",
            referer=FII_REFERER,
            kind="json_api",
        ),
        DownloadTarget(
            name="india_vix",
            urls=[ALL_INDICES_URL],
            output_name=f"india_vix_{ddmmyyyy}.json",
            table_name="india_vix",
            referer=NSE_HOME,
            kind="json_api",
        ),
        DownloadTarget(
            name="equity_bhavcopy",
            urls=[
                *archive_urls(f"content/cm/{cm_udiff_name}"),
                cm_daily_report_url("CM-UDiFF Common Bhavcopy Final (zip)", day),
                cm_daily_report_url("CM - UDiFF Common Bhavcopy Final (zip)", day),
                cm_daily_report_url("CM - Bhavcopy(csv)", day),
                *archive_urls(f"content/historical/EQUITIES/{p['yyyy']}/{p['mon']}/cm{p['dd']}{p['mon']}{p['yyyy']}bhav.csv.zip"),
            ],
            output_name=cm_udiff_name,
            table_name="equity_bhavcopy",
            referer=EQUITIES_REFERER,
        ),
        DownloadTarget(
            name="fo_bhavcopy",
            urls=[
                fo_daily_report_url("F&O - UDiFF Common Bhavcopy Final (zip)", day),
                *archive_urls(f"content/fo/{fo_udiff_name}"),
                fo_daily_report_url("F&O - Bhavcopy(csv)", day),
                fo_daily_report_url("F&O - Bhavcopy File(csv)", day),
                *archive_urls(f"content/historical/DERIVATIVES/{p['yyyy']}/{p['mon']}/fo{p['dd']}{p['mon']}{p['yyyy']}bhav.csv.zip"),
            ],
            output_name=fo_udiff_name,
            table_name="fo_bhavcopy",
            referer=DERIVATIVES_REFERER,
        ),
        DownloadTarget(
            name="delivery_report",
            urls=[
                cm_daily_report_url("CM - Security-wise Delivery Positions", day),
                cm_daily_report_url("Full Bhavcopy and Security Deliverable data", day),
                *archive_urls(f"products/content/sec_bhavdata_full_{ddmmyyyy}.csv"),
            ],
            output_name=f"sec_bhavdata_full_{ddmmyyyy}.csv",
            table_name="delivery_report",
            referer=EQUITIES_REFERER,
        ),
        DownloadTarget(
            name="participant_oi",
            urls=[
                fo_daily_report_url("F&O - Participant wise Open Interest(csv)", day),
                *archive_urls(f"content/nsccl/fao_participant_oi_{ddmmyyyy}.csv"),
            ],
            output_name=f"fao_participant_oi_{ddmmyyyy}.csv",
            table_name="participant_oi",
            referer=DERIVATIVES_REFERER,
        ),
        DownloadTarget(
            name="participant_vol",
            urls=[
                fo_daily_report_url("F&O - Participant wise Trading Volumes(csv)", day),
                *archive_urls(f"content/nsccl/fao_participant_vol_{ddmmyyyy}.csv"),
            ],
            output_name=f"fao_participant_vol_{ddmmyyyy}.csv",
            table_name="participant_vol",
            referer=DERIVATIVES_REFERER,
        ),
        DownloadTarget(
            name="mkt_activity_report",
            urls=[
                cm_daily_report_url("CM - Market Activity Report", day),
                cm_daily_report_url("Market Activity Report", day),
                *archive_urls(f"archives/equities/mkt/MA{ddmmyyyy}.csv"),
            ],
            output_name=f"MA{ddmmyyyy}.csv",
            table_name="market_activity",
            referer=EQUITIES_REFERER,
        ),
    ]


def download_json_api(
    session: requests.Session,
    target: DownloadTarget,
    output_dir: Path,
    day: date,
) -> Dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / target.output_name
    errors: List[str] = []
    for url in target.urls:
        try:
            response = session.get(url, timeout=30, headers={"Referer": target.referer})
            if response.status_code != 200:
                errors.append(f"{response.status_code} {url}")
                continue
            payload = response.json()
            if target.name == "fii_dii":
                if not isinstance(payload, list) or not payload:
                    errors.append(f"empty fii_dii {url}")
                    continue
            elif target.name == "india_vix":
                rows = payload.get("data") if isinstance(payload, dict) else None
                vix = next(
                    (row for row in rows or [] if str(row.get("indexSymbol", "")).upper() == "INDIA VIX"),
                    None,
                )
                if not vix:
                    errors.append(f"india vix missing in {url}")
                    continue
                payload = vix
            output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return {
                "name": target.name,
                "ok": True,
                "url": url,
                "path": str(output_path),
                "bytes": output_path.stat().st_size,
            }
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {url}: {exc}")
    return {"name": target.name, "ok": False, "path": str(output_path), "errors": errors}


def download_first_available(
    session: requests.Session,
    target: DownloadTarget,
    output_dir: Path,
) -> Dict[str, object]:
    if target.kind == "json_api":
        return download_json_api(session, target, output_dir, day=date.today())
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / target.output_name
    errors: List[str] = []
    for url in target.urls:
        try:
            response = session.get(url, timeout=30, headers={"Referer": target.referer})
            if is_valid_download(response):
                output_path.write_bytes(response.content)
                return {
                    "name": target.name,
                    "ok": True,
                    "url": url,
                    "path": str(output_path),
                    "bytes": len(response.content),
                }
            errors.append(f"{response.status_code} {url}")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {url}: {exc}")
    return {"name": target.name, "ok": False, "path": str(output_path), "errors": errors}


def read_delivery_report(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    header_idx = next((idx for idx, line in enumerate(lines) if line.startswith("Record Type,")), None)
    if header_idx is None:
        return read_csv_from_path(path)
    columns = [
        "Record Type",
        "Sr No",
        "Name of Security",
        "Series",
        "Quantity Traded",
        "Deliverable Quantity(gross across client level)",
        "% of Deliverable Quantity to Traded Quantity",
    ]
    return pd.read_csv(
        io.StringIO("\n".join(lines[header_idx:])),
        names=columns,
        header=0,
    )


def read_csv_from_path(path: Path, target_name: str = "") -> Optional[pd.DataFrame]:
    if target_name == "delivery_report" and path.suffix.lower() == ".csv":
        return read_delivery_report(path)
    if not path.exists() or path.stat().st_size == 0:
        return None
    suffixes = "".join(path.suffixes).lower()
    try:
        if suffixes.endswith(".zip"):
            with zipfile.ZipFile(path) as archive:
                names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
                if not names:
                    return None
                with archive.open(names[0]) as handle:
                    return pd.read_csv(handle)
        return pd.read_csv(path)
    except Exception:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
            rows = list(csv.reader(io.StringIO(text)))
            if not rows:
                return None
            max_cols = max(len(row) for row in rows)
            normalized = [row + [""] * (max_cols - len(row)) for row in rows]
            return pd.DataFrame(normalized)
        except Exception:
            return None


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    new_cols: List[str] = []
    for index, col in enumerate(out.columns):
        name = str(col).strip().lower().replace(" ", "_").replace("-", "_").replace("%", "pct")
        if not name or name.isdigit():
            name = f"col_{index}"
        new_cols.append(name)
    out.columns = new_cols
    return out


def read_json_table(path: Path, target_name: str) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if target_name == "fii_dii":
        return pd.DataFrame(payload)
    if target_name == "india_vix":
        return pd.DataFrame([payload])
    return None


def import_to_sqlite(
    db_path: Path,
    table_name: str,
    day: date,
    file_path: Path,
    target_name: str = "",
) -> Dict[str, object]:
    if file_path.suffix.lower() == ".json":
        df = read_json_table(file_path, target_name or table_name)
    else:
        df = read_csv_from_path(file_path, target_name=target_name or table_name)
    if df is None or df.empty:
        return {"table": table_name, "ok": False, "rows": 0, "reason": "empty_or_unreadable"}
    df = normalize_columns(df)
    df.insert(0, "trade_date", day.isoformat())
    df.insert(1, "source_file", file_path.name)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        if exists:
            existing_cols = [
                row[1]
                for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            ]
            if existing_cols != list(df.columns):
                conn.execute(f"DROP TABLE {table_name}")
                exists = None
            else:
                conn.execute(
                    f"DELETE FROM {table_name} WHERE trade_date = ? AND source_file = ?",
                    (day.isoformat(), file_path.name),
                )
        df.to_sql(table_name, conn, if_exists="append", index=False)
    return {"table": table_name, "ok": True, "rows": int(len(df))}


def load_manifest(raw_day_dir: Path) -> Optional[Dict[str, object]]:
    manifest_path = raw_day_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def parse_date(value: str) -> date:
    if not value:
        return date.today()
    return datetime.strptime(value, "%Y-%m-%d").date()


def previous_trading_day(start: Optional[date] = None) -> date:
    day = start or date.today()
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def record_download_run(db_path: Path, manifest: Dict[str, object]) -> None:
    init_db(db_path)
    downloads = manifest.get("downloads") or []
    ok_count = sum(1 for row in downloads if row.get("ok"))
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO download_runs (trade_date, started_at, finished_at, ok_count, total_count, manifest_path, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                manifest.get("trade_date"),
                manifest.get("started_at"),
                manifest.get("generated_at"),
                ok_count,
                len(downloads),
                str((Path(str(manifest.get("raw_dir", ""))) / "manifest.json")),
                json.dumps(PUBLISH_WINDOWS),
            ),
        )
        conn.commit()


def run_download(
    day: date,
    raw_dir: Path,
    db_path: Path,
    no_import: bool,
    only_targets: Optional[Iterable[str]] = None,
    retry_missing: bool = False,
    import_only: bool = False,
) -> Dict[str, object]:
    raw_day_dir = raw_dir / day.isoformat()
    existing = load_manifest(raw_day_dir) if retry_missing or import_only else None
    failed_names = set()
    if existing and retry_missing:
        failed_names = {row.get("name") for row in existing.get("downloads", []) if not row.get("ok")}

    manifest: Dict[str, object] = {
        "trade_date": day.isoformat(),
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "generated_at": "",
        "raw_dir": str(raw_day_dir),
        "db_path": str(db_path),
        "publish_windows": PUBLISH_WINDOWS,
        "downloads": existing.get("downloads", []) if import_only and existing else [],
        "imports": [],
    }
    if import_only and existing:
        manifest["downloads"] = existing.get("downloads", [])

    init_db(db_path)
    if import_only:
        existing = load_manifest(raw_day_dir)
        if not existing:
            raise SystemExit(f"No manifest found for import-only: {raw_day_dir / 'manifest.json'}")
        manifest["downloads"] = existing.get("downloads", [])
        for row in manifest["downloads"]:
            if not row.get("ok"):
                continue
            target = next((item for item in build_targets(day) if item.name == row.get("name")), None)
            if target and target.table_name and not no_import:
                import_result = import_to_sqlite(
                    db_path,
                    target.table_name,
                    day,
                    Path(str(row["path"])),
                    target_name=target.name,
                )
                manifest["imports"].append(import_result)
        manifest["generated_at"] = datetime.now().isoformat(timespec="seconds")
        raw_day_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = raw_day_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        record_download_run(db_path, manifest)
        return manifest

    session = nse_session()
    targets = build_targets(day)
    if only_targets:
        allowed = {name.strip() for name in only_targets}
        targets = [target for target in targets if target.name in allowed]

    downloads_by_name: Dict[str, object] = {}
    if existing:
        downloads_by_name = {str(row.get("name")): row for row in existing.get("downloads", [])}

    for target in targets:
        if retry_missing and target.name not in failed_names:
            continue
        result = download_first_available(session, target, raw_day_dir)
        downloads_by_name[target.name] = result

        if result.get("ok") and target.table_name and not no_import:
            import_result = import_to_sqlite(
                db_path,
                target.table_name,
                day,
                Path(str(result["path"])),
                target_name=target.name,
            )
            manifest["imports"].append(import_result)

    manifest["downloads"] = list(downloads_by_name.values())
    manifest["generated_at"] = datetime.now().isoformat(timespec="seconds")
    raw_day_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = raw_day_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    record_download_run(db_path, manifest)
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download NSE EOD data into raw files and SQLite")
    parser.add_argument("--date", default="", help="Trade date YYYY-MM-DD. Default: today")
    parser.add_argument("--previous", action="store_true", help="Use previous weekday")
    parser.add_argument("--raw-dir", default=str(RAW_DIR), help="Raw output directory")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite DB path")
    parser.add_argument("--no-import", action="store_true", help="Only download raw files")
    parser.add_argument("--import-only", action="store_true", help="Import existing raw files for date")
    parser.add_argument("--retry-missing", action="store_true", help="Retry only targets that failed in manifest")
    parser.add_argument("--targets", default="", help="Comma-separated target names (fii_dii,fo_bhavcopy,...)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    day = previous_trading_day() if args.previous else parse_date(args.date)
    only_targets = [part.strip() for part in args.targets.split(",") if part.strip()] or None
    manifest = run_download(
        day=day,
        raw_dir=Path(args.raw_dir),
        db_path=Path(args.db),
        no_import=args.no_import,
        only_targets=only_targets,
        retry_missing=args.retry_missing,
        import_only=args.import_only,
    )
    ok_count = sum(1 for row in manifest["downloads"] if row.get("ok"))
    print(f"NSE EOD complete for {day.isoformat()}: {ok_count}/{len(manifest['downloads'])} files")
    print(f"Manifest: {Path(str(manifest['raw_dir'])) / 'manifest.json'}")
    if not args.no_import:
        print(f"SQLite: {args.db}")
        imported = [row for row in manifest.get("imports", []) if row.get("ok")]
        if imported:
            print("Imported:", ", ".join(f"{row['table']}({row['rows']})" for row in imported))


if __name__ == "__main__":
    main()
