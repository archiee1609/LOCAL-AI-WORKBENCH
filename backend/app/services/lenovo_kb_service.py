"""
Lenovo Tips & Resources Knowledge Base Service.
Automatically ingests workbooks and documents from `Tips & Resources` (with schema-aware
adapters for AP Services Known Issues, Known_Tips, LEAP Tips, and Thursday Tech Tips),
extracts cell hyperlinks and embedded SOP images, deduplicates overlapping records,
maintains a built-in SQLite FTS5 index, and serves hybrid retrieval for chat & explorer UI.
"""

import csv
import datetime
import hashlib
import io
import json
import re
import shutil
import threading
import time
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import httpx
import openpyxl

from app.core.config import settings
from app.core.logging import logger
from app.database.connection import db

SUPPORTED_KB_EXTENSIONS = {".xlsx", ".xls", ".csv", ".pdf", ".docx", ".md", ".txt"}

SOURCE_TYPE_LABELS = {
    "known_issue": "🛠️ AP Known Issue",
    "known_tip": "💡 Known Tip",
    "leap_tip": "🚀 LEAP Tip",
    "tech_tip": "📅 Thursday Tech Tip",
    "video_guide": "🎬 Part Removal Video",
    "document": "📄 Resource Document",
}

LENOVO_ALIAS_MAP: List[Tuple[str, str]] = [
    (r"\btp\b", "ThinkPad"),
    (r"\bip\s*3\b|\bip3\b", "IdeaPad 3 IP3"),
    (r"\bip\s*5\b|\bip5\b", "IdeaPad 5 IP5"),
    (r"\bip\b", "IdeaPad"),
    (r"\btb\b", "ThinkBook"),
    (r"\bic\b", "IdeaCentre"),
    (r"\btc\b", "ThinkCentre"),
    (r"\bx1c\b", "X1 Carbon"),
    (r"\bmbd\b", "Motherboard System Board"),
    (r"\blcm\b", "LCD Panel Display"),
    (r"\bqi\b", "Quality Issue QI"),
    (r"\bg(\d+)\b", r"Gen \1 G\1"),
    (r"\bgen\s*(\d+)\b", r"Gen \1 G\1"),
]

_REVERSE_ALIAS_TOKENS: Dict[str, List[str]] = {
    "thinkpad": ["tp", "thinkpad"],
    "tp": ["thinkpad", "tp"],
    "ideapad": ["ip", "ideapad"],
    "ip": ["ideapad", "ip"],
    "ip3": ["ideapad", "3", "ip3"],
    "thinkbook": ["tb", "thinkbook"],
    "tb": ["thinkbook", "tb"],
    "ideacentre": ["ic", "ideacentre"],
    "thinkcentre": ["tc", "thinkcentre"],
    "motherboard": ["mbd", "motherboard", "system board"],
    "mbd": ["mbd", "motherboard"],
    "slim": ["slim", "ips5", "ips3"],
    "ips5": ["slim", "5", "ideapad", "ips5"],
    "ips3": ["slim", "3", "ideapad", "ips3"],
    "carbon": ["carbon", "x1c"],
    "x1c": ["x1", "carbon", "x1c"],
}

_MT_REGEX = re.compile(r"\b(\d{2}(?=[A-Z0-9]*[A-Z])[A-Z0-9]{2})\b", re.IGNORECASE)
_TYPE_MT_REGEX = re.compile(r"\bType\s+([A-Z0-9,\s/\-]+)", re.IGNORECASE)
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "could", "do", "does",
    "for", "from", "get", "give", "have", "has", "how", "i", "in", "is", "it", "its",
    "know", "me", "my", "of", "on", "or", "please", "show", "tell", "that", "the",
    "their", "there", "these", "this", "to", "what", "when", "where", "which", "who",
    "why", "with", "would", "you", "your", "about", "any", "all", "related", "issue",
    "issues", "tips", "tip", "problem", "problems", "help", "find", "search", "look",
    "known", "lenovo", "laptop", "system", "model", "models", "pc", "machine",
}

_ENSURE_KB_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kb_sources (
    file_name TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    file_size INTEGER DEFAULT 0,
    mtime REAL DEFAULT 0.0,
    record_count INTEGER DEFAULT 0,
    asset_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'indexed',
    error_msg TEXT DEFAULT '',
    last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS kb_entries (
    id TEXT PRIMARY KEY,
    canonical_key TEXT NOT NULL,
    source_file TEXT NOT NULL,
    source_files_json TEXT DEFAULT '[]',
    sheet_name TEXT DEFAULT '',
    row_number INTEGER DEFAULT 0,
    source_type TEXT DEFAULT 'known_tip',
    issue_code TEXT DEFAULT '',
    model_names TEXT DEFAULT '',
    machine_types TEXT DEFAULT '',
    segment TEXT DEFAULT '',
    vendor TEXT DEFAULT '',
    commodity TEXT DEFAULT '',
    fru_parts TEXT DEFAULT '',
    buffer_parts TEXT DEFAULT '',
    pd_code TEXT DEFAULT '',
    qdts_code TEXT DEFAULT '',
    critsit TEXT DEFAULT '',
    applicable_market TEXT DEFAULT '',
    confirmed_qi TEXT DEFAULT '',
    issue_status TEXT DEFAULT '',
    impact TEXT DEFAULT '',
    title TEXT DEFAULT '',
    symptoms TEXT DEFAULT '',
    root_cause TEXT DEFAULT '',
    resolution TEXT DEFAULT '',
    extra_notes TEXT DEFAULT '',
    links_json TEXT DEFAULT '[]',
    assets_json TEXT DEFAULT '[]',
    record_date TEXT DEFAULT '',
    quarter TEXT DEFAULT '',
    search_blob TEXT DEFAULT '',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE VIRTUAL TABLE IF NOT EXISTS kb_entries_fts USING fts5(
    entry_id UNINDEXED,
    title,
    model_names,
    machine_types,
    symptoms,
    root_cause,
    resolution,
    fru_parts,
    codes,
    search_blob,
    tokenize='porter unicode61'
);

CREATE INDEX IF NOT EXISTS idx_kb_entries_canonical ON kb_entries(canonical_key);
CREATE INDEX IF NOT EXISTS idx_kb_entries_source_file ON kb_entries(source_file);
CREATE INDEX IF NOT EXISTS idx_kb_entries_type ON kb_entries(source_type);
CREATE INDEX IF NOT EXISTS idx_kb_entries_qi ON kb_entries(confirmed_qi);
"""


def _clean_text(val: Any) -> str:
    """Normalizes Excel cell values (dates, non-breaking spaces, zero-width chars)."""
    if val is None:
        return ""
    if isinstance(val, datetime.datetime):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, datetime.date):
        return val.strftime("%Y-%m-%d")
    text = str(val)
    text = text.replace("\xa0", " ").replace("\u200b", " ").replace("\ufeff", "")
    # Collapse excess spaces on each line while keeping meaningful line structure
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def _clean_inline(val: Any) -> str:
    return re.sub(r"\s+", " ", _clean_text(val)).strip()


def _extract_machine_types(*texts: str) -> List[str]:
    """Extracts 4-character Lenovo Machine Type codes (e.g., 21CB, 83DV, 62DC)."""
    found: List[str] = []
    seen: Set[str] = set()
    for t in texts:
        if not t:
            continue
        for m in _MT_REGEX.findall(t):
            code = m.upper()
            if code not in seen:
                seen.add(code)
                found.append(code)
        for grp in _TYPE_MT_REGEX.findall(t):
            for part in re.findall(r"\b([A-Z0-9]{4})\b", grp, re.IGNORECASE):
                code = part.upper()
                if any(ch.isdigit() for ch in code) and code not in seen:
                    seen.add(code)
                    found.append(code)
    return found


def _expand_text_aliases(text: str) -> str:
    """Adds domain alias expansions so abbreviated and full forms both index cleanly."""
    if not text:
        return ""
    extras = [text]
    for pat, repl in LENOVO_ALIAS_MAP:
        if re.search(pat, text, flags=re.IGNORECASE):
            extras.append(re.sub(pat, repl, text, flags=re.IGNORECASE))
    return " | ".join(dict.fromkeys(e.strip() for e in extras if e.strip()))


def _extract_model_hints_from_title(title: str) -> str:
    """Extracts concise model phrase hints from prose titles when explicit model column is absent."""
    if not title:
        return ""
    cleaned = re.sub(r"^(?:QI|KM|HT|Sharing)\s*:\s*", "", title, flags=re.IGNORECASE).strip()
    families = re.findall(
        r"\b(?:ThinkPad|ThinkBook|ThinkCentre|ThinkVision|IdeaPad|IdeaCentre|Yoga|Legion|LOQ|ChromeBook|Tablet|MBG|AI PC)"
        r"(?:\s+[A-Za-z0-9\-\+/]+){0,5}",
        cleaned,
        flags=re.IGNORECASE,
    )
    return ", ".join(dict.fromkeys(f.strip(" .,:;-") for f in families if f.strip()))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class LenovoKBService:
    """Service managing ingestion, incremental sync, deduplication, and hybrid search for Lenovo KB."""

    def __init__(self) -> None:
        self._verified_db_path: Optional[str] = None
        self._sync_lock = threading.Lock()
        self._last_check_ts: float = 0.0
        self._known_mt_cache: Set[str] = set()
        self._cache_db_path: Optional[str] = None
        self._video_cache: Dict[str, Tuple[Dict[str, Any], float]] = {}
        self._video_cache_ttl: float = 3600.0

    def ensure_tables(self) -> None:
        cur_path = str(db.db_path)
        if self._verified_db_path == cur_path:
            return
        try:
            db._executescript_sync(_ENSURE_KB_SCHEMA_SQL)
            self._verified_db_path = cur_path
        except Exception as exc:
            logger.debug(f"[LenovoKBService] ensure_tables notice: {exc}")

    def _get_kb_dir(self, kb_dir: Optional[Path] = None) -> Path:
        default_dir = getattr(settings, "tips_resources_dir", settings.base_dir / "Tips & Resources")
        return Path(kb_dir or default_dir)

    def _get_assets_dir(self, assets_dir: Optional[Path] = None) -> Path:
        default_assets = getattr(settings, "kb_assets_dir", settings.data_dir / "kb_assets")
        target = Path(assets_dir or default_assets)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _list_source_files(self, kb_dir: Path) -> List[Path]:
        if not kb_dir.exists():
            return []
        files: List[Path] = []
        for item in sorted(kb_dir.rglob("*")):
            if not item.is_file():
                continue
            if item.name.startswith("~$") or item.name.startswith("."):
                continue
            if item.suffix.lower() in SUPPORTED_KB_EXTENSIONS:
                files.append(item)
        return files

    def _refresh_known_mt_cache(self) -> None:
        cur_path = str(db.db_path)
        try:
            rows = db._fetchall_sync("SELECT machine_types FROM kb_entries WHERE machine_types != ''")
            mts: Set[str] = set()
            for r in rows:
                for token in re.split(r"[,\s;]+", str(r.get("machine_types") or "")):
                    tok = token.strip().upper()
                    if 3 <= len(tok) <= 6 and tok.isalnum() and any(ch.isdigit() for ch in tok):
                        if not (tok.isdigit() and 2015 <= int(tok) <= 2035):
                            mts.add(tok)
            self._known_mt_cache = mts
            self._cache_db_path = cur_path
        except Exception:
            self._known_mt_cache = set()

    def _extract_ws_images(
        self, ws: Any, source_key: str, assets_dir: Path
    ) -> Tuple[Dict[int, List[Dict[str, Any]]], int]:
        """Extracts embedded images from an openpyxl worksheet mapped by 1-based row index."""
        row_assets: Dict[int, List[Dict[str, Any]]] = {}
        extracted_count = 0
        raw_images = getattr(ws, "_images", None) or []
        if not raw_images:
            return row_assets, 0

        safe_dir = assets_dir / hashlib.md5(source_key.encode("utf-8")).hexdigest()[:12]
        safe_dir.mkdir(parents=True, exist_ok=True)

        for idx, img in enumerate(raw_images, start=1):
            try:
                anchor = getattr(img, "anchor", None)
                from_pos = getattr(anchor, "_from", None)
                row_num = int(from_pos.row) + 1 if from_pos is not None else 0
                if row_num <= 0:
                    continue
                img_bytes = img._data() if callable(getattr(img, "_data", None)) else None
                if not img_bytes:
                    continue
                fmt = str(getattr(img, "format", "png") or "png").lower()
                if fmt not in ("png", "jpeg", "jpg", "webp", "bmp", "gif"):
                    fmt = "png"
                out_name = f"row_{row_num}_img_{idx}.{fmt}"
                out_path = safe_dir / out_name
                out_path.write_bytes(img_bytes)
                row_assets.setdefault(row_num, []).append({
                    "path": str(out_path.resolve()),
                    "row": row_num,
                    "format": fmt,
                    "width": int(getattr(img, "width", 0) or 0),
                    "height": int(getattr(img, "height", 0) or 0),
                })
                extracted_count += 1
            except Exception as exc:
                logger.debug(f"[LenovoKBService] Skipped image extraction in {source_key}: {exc}")
        return row_assets, extracted_count

    def _extract_row_hyperlinks(self, ws: Any, row_idx: int, max_col: int) -> List[Dict[str, str]]:
        links: List[Dict[str, str]] = []
        seen_urls: Set[str] = set()
        for col_idx in range(1, max_col + 1):
            cell = ws.cell(row_idx, col_idx)
            val_str = _clean_inline(cell.value)
            hl_target = ""
            if getattr(cell, "hyperlink", None) and getattr(cell.hyperlink, "target", None):
                hl_target = str(cell.hyperlink.target).strip()
            if hl_target and hl_target not in seen_urls:
                seen_urls.add(hl_target)
                links.append({"label": val_str or "Reference Link", "url": hl_target})
            elif val_str.startswith(("http://", "https://")) and val_str not in seen_urls:
                seen_urls.add(val_str)
                links.append({"label": "Reference Link", "url": val_str})
        return links

    def _parse_ap_or_leap_sheet(
        self,
        ws: Any,
        rel_name: str,
        sheet_name: str,
        source_type: str,
        row_assets: Dict[int, List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        headers: Dict[int, str] = {}
        for col_idx in range(1, ws.max_column + 1):
            h = _clean_inline(ws.cell(1, col_idx).value)
            if h:
                headers[col_idx] = h.lower()

        def find_col(*candidates: str) -> Optional[int]:
            for cand in candidates:
                cl = cand.lower()
                for c_idx, h_val in headers.items():
                    if h_val == cl or cl in h_val:
                        return c_idx
            return None

        c_issue_num = find_col("issue #", "issue#")
        c_start = find_col("start date")
        c_closed = find_col("closed/update date", "update date")
        c_status = find_col("status")
        c_qi = find_col("confirmed qi")
        c_seg = find_col("segment")
        c_vendor = find_col("vendor")
        c_issues = find_col("issues", "issue title", "issue")
        c_impact = find_col("impact")
        c_mt = find_col("mt")
        c_rc = find_col("root cause")
        c_comm_old = find_col("commodity name - old")
        c_comm_new = find_col("commodity name - lsump")
        c_fru = find_col("impactfru", "impact fru")
        c_buf = find_col("buffer parts")
        c_mkt = find_col("appliable market", "impact market", "case report market")
        c_remark = find_col("remark", "resolution")
        c_crit = find_col("critsit")
        c_qdts = find_col("qdts")
        c_pd_code = find_col("pd code")
        c_pd_symp = find_col("pd symptom")
        c_tips = find_col("tips#")

        records: List[Dict[str, Any]] = []
        for r_idx in range(2, ws.max_row + 1):
            issue_text = _clean_text(ws.cell(r_idx, c_issues).value) if c_issues else ""
            remark_text = _clean_text(ws.cell(r_idx, c_remark).value) if c_remark else ""
            mt_raw = _clean_text(ws.cell(r_idx, c_mt).value) if c_mt else ""
            if not issue_text and not remark_text and not mt_raw:
                continue

            issue_num = _clean_inline(ws.cell(r_idx, c_issue_num).value) if c_issue_num else ""
            tips_num = _clean_inline(ws.cell(r_idx, c_tips).value) if c_tips else ""
            issue_code = issue_num or tips_num
            start_dt = _clean_inline(ws.cell(r_idx, c_start).value) if c_start else ""
            closed_dt = _clean_inline(ws.cell(r_idx, c_closed).value) if c_closed else ""
            record_date = closed_dt or start_dt
            status_val = _clean_inline(ws.cell(r_idx, c_status).value) if c_status else ""
            qi_val = _clean_inline(ws.cell(r_idx, c_qi).value) if c_qi else ""
            seg_val = _clean_inline(ws.cell(r_idx, c_seg).value) if c_seg else ""
            vendor_val = _clean_inline(ws.cell(r_idx, c_vendor).value) if c_vendor else ""
            impact_val = _clean_inline(ws.cell(r_idx, c_impact).value) if c_impact else ""
            rc_val = _clean_text(ws.cell(r_idx, c_rc).value) if c_rc else ""
            comm_val = " / ".join(
                dict.fromkeys(
                    x for x in [
                        _clean_inline(ws.cell(r_idx, c_comm_new).value) if c_comm_new else "",
                        _clean_inline(ws.cell(r_idx, c_comm_old).value) if c_comm_old else "",
                    ] if x
                )
            )
            fru_val = _clean_inline(ws.cell(r_idx, c_fru).value) if c_fru else ""
            buf_val = _clean_inline(ws.cell(r_idx, c_buf).value) if c_buf else ""
            mkt_val = _clean_inline(ws.cell(r_idx, c_mkt).value) if c_mkt else ""
            crit_val = _clean_inline(ws.cell(r_idx, c_crit).value) if c_crit else ""
            qdts_val = _clean_inline(ws.cell(r_idx, c_qdts).value) if c_qdts else ""
            pd_code_val = _clean_inline(ws.cell(r_idx, c_pd_code).value) if c_pd_code else ""
            pd_symp_val = _clean_inline(ws.cell(r_idx, c_pd_symp).value) if c_pd_symp else ""

            mts = _extract_machine_types(mt_raw, issue_text, remark_text)
            model_hints = _extract_model_hints_from_title(issue_text)
            model_names = ", ".join(
                dict.fromkeys(x for x in [model_hints, _clean_inline(mt_raw).replace("•", " ")] if x)
            )[:350]
            links = self._extract_row_hyperlinks(ws, r_idx, min(ws.max_column, 35))
            assets = row_assets.get(r_idx, [])

            symptoms = issue_text
            if pd_symp_val and pd_symp_val.upper() != "NIL":
                symptoms = f"{issue_text}\nPD Symptom: {pd_symp_val}" if issue_text else pd_symp_val

            extra_parts = []
            if tips_num and issue_num:
                extra_parts.append(f"Tips#: {tips_num}")
            if start_dt and closed_dt and start_dt != closed_dt:
                extra_parts.append(f"Start Date: {start_dt}")

            norm_title = re.sub(r"\W+", " ", issue_text.lower()).strip()[:120]
            norm_mt = ",".join(sorted(mts))
            norm_res = re.sub(r"\W+", " ", remark_text.lower()).strip()[:100]
            canonical_key = hashlib.sha1(f"{norm_title}|{norm_mt}|{norm_res}".encode("utf-8")).hexdigest()

            records.append({
                "canonical_key": canonical_key,
                "source_file": rel_name,
                "sheet_name": sheet_name,
                "row_number": r_idx,
                "source_type": source_type,
                "issue_code": issue_code,
                "model_names": model_names,
                "machine_types": ", ".join(mts),
                "segment": seg_val,
                "vendor": vendor_val,
                "commodity": comm_val,
                "fru_parts": fru_val,
                "buffer_parts": buf_val,
                "pd_code": pd_code_val,
                "qdts_code": qdts_val,
                "critsit": crit_val,
                "applicable_market": mkt_val,
                "confirmed_qi": qi_val,
                "issue_status": status_val,
                "impact": impact_val,
                "title": issue_text.splitlines()[0][:240] if issue_text else f"{model_names} Known Issue",
                "symptoms": symptoms,
                "root_cause": rc_val,
                "resolution": remark_text,
                "extra_notes": " | ".join(extra_parts),
                "links": links,
                "assets": assets,
                "record_date": record_date,
                "quarter": "",
            })
        return records

    def _parse_known_tips_sheet(
        self,
        ws: Any,
        rel_name: str,
        sheet_name: str,
        row_assets: Dict[int, List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        for r_idx in range(2, ws.max_row + 1):
            model_val = _clean_inline(ws.cell(r_idx, 1).value)
            issue_val = _clean_text(ws.cell(r_idx, 2).value)
            res_val = _clean_text(ws.cell(r_idx, 3).value)
            extra_vals = [
                _clean_inline(ws.cell(r_idx, c).value)
                for c in range(4, min(ws.max_column + 1, 12))
                if ws.cell(r_idx, c).value is not None and _clean_inline(ws.cell(r_idx, c).value)
            ]
            links = self._extract_row_hyperlinks(ws, r_idx, min(ws.max_column, 12))
            assets = row_assets.get(r_idx, [])

            if not model_val and not issue_val and not res_val and not assets:
                continue

            mts = _extract_machine_types(model_val, issue_val, res_val)
            fru_matches = re.findall(r"\b(?:5[A-Z0-9]{9}|PD[A-Z0-9\-]{5,})\b", f"{res_val} {issue_val}", re.IGNORECASE)
            fru_str = ", ".join(dict.fromkeys(m.upper() for m in fru_matches if not m.upper().startswith("PD")))
            pd_str = ", ".join(dict.fromkeys(m.upper() for m in fru_matches if m.upper().startswith("PD")))

            title_short = issue_val.splitlines()[0][:180] if issue_val else "Service Tip"
            full_title = f"{model_val} — {title_short}" if model_val else title_short
            canon_src = f"kt|{model_val.lower()}|{title_short.lower()}"
            canonical_key = hashlib.sha1(canon_src.encode("utf-8")).hexdigest()

            records.append({
                "canonical_key": canonical_key,
                "source_file": rel_name,
                "sheet_name": sheet_name,
                "row_number": r_idx,
                "source_type": "known_tip",
                "issue_code": f"KT-{r_idx - 1:03d}",
                "model_names": model_val,
                "machine_types": ", ".join(mts),
                "segment": "",
                "vendor": "Lenovo",
                "commodity": "",
                "fru_parts": fru_str,
                "buffer_parts": "",
                "pd_code": pd_str,
                "qdts_code": "",
                "critsit": "",
                "applicable_market": "",
                "confirmed_qi": "",
                "issue_status": "Active Tip",
                "impact": "",
                "title": full_title,
                "symptoms": issue_val,
                "root_cause": "",
                "resolution": res_val,
                "extra_notes": " | ".join(extra_vals),
                "links": links,
                "assets": assets,
                "record_date": "",
                "quarter": "",
            })
        return records

    def _parse_thursday_tech_tips_sheet(
        self, ws: Any, rel_name: str, sheet_name: str
    ) -> List[Dict[str, Any]]:
        # Detect real header row (Row 2 when Row 1 starts with Column1 placeholder)
        r1_first = _clean_inline(ws.cell(1, 1).value).lower()
        r2_vals = [_clean_inline(ws.cell(2, c).value).lower() for c in range(1, min(ws.max_column + 1, 12))]
        header_row = 2 if (r1_first.startswith("column") or "course title" in r2_vals or "course code" in r2_vals) else 1

        headers: Dict[int, str] = {}
        for col_idx in range(1, min(ws.max_column + 1, 20)):
            h = _clean_inline(ws.cell(header_row, col_idx).value)
            if h:
                headers[col_idx] = h.lower()

        def find_col(*candidates: str) -> Optional[int]:
            for cand in candidates:
                cl = cand.lower()
                for c_idx, h_val in headers.items():
                    if cl in h_val:
                        return c_idx
            return None

        c_jp = find_col("japanese")
        c_code = find_col("course code")
        c_title = find_col("course title", "title")
        c_link = find_col("leap link", "link")
        c_quarter = find_col("quarter")
        c_created = find_col("creation time")
        c_updated = find_col("update time")

        records: List[Dict[str, Any]] = []
        for r_idx in range(header_row + 1, ws.max_row + 1):
            course_title = _clean_inline(ws.cell(r_idx, c_title).value) if c_title else ""
            course_code = _clean_inline(ws.cell(r_idx, c_code).value) if c_code else ""
            if not course_title and not course_code:
                continue

            jp_flag = _clean_inline(ws.cell(r_idx, c_jp).value) if c_jp else ""
            quarter_val = _clean_inline(ws.cell(r_idx, c_quarter).value) if c_quarter else ""
            created_val = _clean_inline(ws.cell(r_idx, c_created).value) if c_created else ""
            updated_val = _clean_inline(ws.cell(r_idx, c_updated).value) if c_updated else ""
            links = self._extract_row_hyperlinks(ws, r_idx, min(ws.max_column, 12))
            if c_link:
                raw_link_txt = _clean_inline(ws.cell(r_idx, c_link).value)
                if raw_link_txt.startswith(("http://", "https://")) and not any(l["url"] == raw_link_txt for l in links):
                    links.insert(0, {"label": "LEAP Link", "url": raw_link_txt})

            extra_codes = [
                _clean_inline(ws.cell(r_idx, col_i).value)
                for col_i in (8, 9, 19)
                if col_i <= ws.max_column and ws.cell(r_idx, col_i).value is not None and _clean_inline(ws.cell(r_idx, col_i).value)
            ]
            mts = _extract_machine_types(course_title)
            model_hints = _extract_model_hints_from_title(course_title)

            extra_str_parts = []
            if extra_codes:
                extra_str_parts.append(f"Ref Codes: {', '.join(extra_codes)}")
            if jp_flag:
                extra_str_parts.append(f"Japanese Version: {jp_flag}")

            res_summary = f"Thursday Tech Tip {course_code}".strip()
            if quarter_val:
                res_summary += f" ({quarter_val})"
            if links:
                res_summary += f" — Official LEAP guide available at {links[0]['url']}"
            if extra_codes:
                res_summary += f" [Ref: {', '.join(extra_codes)}]"

            canon_src = f"ttt|{course_code.lower()}|{course_title.lower()}"
            canonical_key = hashlib.sha1(canon_src.encode("utf-8")).hexdigest()

            records.append({
                "canonical_key": canonical_key,
                "source_file": rel_name,
                "sheet_name": sheet_name,
                "row_number": r_idx,
                "source_type": "tech_tip",
                "issue_code": course_code,
                "model_names": model_hints,
                "machine_types": ", ".join(mts),
                "segment": "",
                "vendor": "Lenovo",
                "commodity": "",
                "fru_parts": "",
                "buffer_parts": "",
                "pd_code": "",
                "qdts_code": "",
                "critsit": "",
                "applicable_market": "",
                "confirmed_qi": "",
                "issue_status": quarter_val or "Published",
                "impact": "",
                "title": course_title,
                "symptoms": course_title,
                "root_cause": "",
                "resolution": res_summary,
                "extra_notes": " | ".join(extra_str_parts),
                "links": links,
                "assets": [],
                "record_date": updated_val or created_val,
                "quarter": quarter_val,
            })
        return records

    def _parse_generic_sheet(
        self,
        ws: Any,
        rel_name: str,
        sheet_name: str,
        row_assets: Dict[int, List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """Fallback tabular parser for newly added future Excel sheets with arbitrary columns."""
        # Find first row with >=2 non-placeholder string headers
        header_row = 1
        for r in range(1, min(ws.max_row + 1, 6)):
            cells = [_clean_inline(ws.cell(r, c).value) for c in range(1, min(ws.max_column + 1, 30))]
            non_empty = [x for x in cells if x]
            if len(non_empty) >= 2 and not all(x.lower().startswith("column") for x in non_empty):
                header_row = r
                break

        headers = {
            c: _clean_inline(ws.cell(header_row, c).value)
            for c in range(1, min(ws.max_column + 1, 35))
            if _clean_inline(ws.cell(header_row, c).value)
        }
        if not headers:
            return []

        records: List[Dict[str, Any]] = []
        for r_idx in range(header_row + 1, ws.max_row + 1):
            row_dict: Dict[str, str] = {}
            for c_idx, h_name in headers.items():
                val = _clean_text(ws.cell(r_idx, c_idx).value)
                if val:
                    row_dict[h_name] = val
            if not row_dict:
                continue

            combined = " ".join(row_dict.values())
            links = self._extract_row_hyperlinks(ws, r_idx, min(ws.max_column, 35))
            assets = row_assets.get(r_idx, [])

            def pick(*keys: str) -> str:
                for k in keys:
                    for hk, hv in row_dict.items():
                        if k in hk.lower():
                            return hv
                return ""

            model_val = pick("model", "product", "device") or _extract_model_hints_from_title(combined)
            issue_val = pick("issue", "symptom", "title", "topic", "subject", "problem")
            res_val = pick("resolution", "solution", "remark", "fix", "workaround", "action")
            rc_val = pick("root cause", "cause")
            code_val = pick("issue #", "code", "course code", "tip#", "id")
            mt_val = pick("mt", "machine type")
            fru_val = pick("fru", "part")
            mts = _extract_machine_types(mt_val, model_val, combined)

            title = issue_val.splitlines()[0][:200] if issue_val else f"{rel_name} Row {r_idx}"
            if not res_val:
                res_val = " | ".join(f"{hk}: {hv}" for hk, hv in list(row_dict.items())[:6])

            canon_src = f"gen|{rel_name}|{sheet_name}|{r_idx}|{title.lower()}"
            records.append({
                "canonical_key": hashlib.sha1(canon_src.encode("utf-8")).hexdigest(),
                "source_file": rel_name,
                "sheet_name": sheet_name,
                "row_number": r_idx,
                "source_type": "document",
                "issue_code": code_val[:60],
                "model_names": model_val[:300],
                "machine_types": ", ".join(mts),
                "segment": pick("segment")[:80],
                "vendor": pick("vendor")[:80],
                "commodity": pick("commodity")[:120],
                "fru_parts": fru_val[:120],
                "buffer_parts": pick("buffer")[:80],
                "pd_code": pick("pd code")[:80],
                "qdts_code": pick("qdts")[:80],
                "critsit": pick("critsit")[:80],
                "applicable_market": pick("market")[:80],
                "confirmed_qi": pick("confirmed qi")[:20],
                "issue_status": pick("status")[:40],
                "impact": pick("impact")[:40],
                "title": title,
                "symptoms": issue_val or combined[:500],
                "root_cause": rc_val,
                "resolution": res_val,
                "extra_notes": "",
                "links": links,
                "assets": assets,
                "record_date": pick("date", "update time", "creation time")[:20],
                "quarter": pick("quarter")[:20],
            })
        return records

    def _parse_video_links_sheet(
        self, ws: Any, rel_name: str, sheet_name: str
    ) -> List[Dict[str, Any]]:
        """Parses MT -> Model -> Hyperlink Part Removal Video mappings cleanly."""
        headers: Dict[int, str] = {}
        for col_idx in range(1, min(ws.max_column + 1, 15)):
            h = _clean_inline(ws.cell(1, col_idx).value)
            if h:
                headers[col_idx] = h.lower()

        def find_col(*candidates: str) -> Optional[int]:
            for cand in candidates:
                cl = cand.lower()
                for c_idx, h_val in headers.items():
                    if h_val == cl or cl in h_val:
                        return c_idx
            return None

        c_mt = find_col("mt", "machine type")
        c_model = find_col("model", "product")
        c_link = find_col("hyperlink", "video link", "video", "link", "url")

        records: List[Dict[str, Any]] = []
        for r_idx in range(2, ws.max_row + 1):
            mt_val = _clean_inline(ws.cell(r_idx, c_mt).value) if c_mt else ""
            model_val = _clean_inline(ws.cell(r_idx, c_model).value) if c_model else ""
            url_val = _clean_inline(ws.cell(r_idx, c_link).value) if c_link else ""
            if c_link and not url_val:
                hl = getattr(ws.cell(r_idx, c_link), "hyperlink", None)
                if hl and getattr(hl, "target", None):
                    url_val = str(hl.target).strip()

            if not model_val and not url_val and not mt_val:
                continue

            mt_list: List[str] = []
            if mt_val:
                clean_mt = mt_val.upper()
                mt_list.append(clean_mt)
                if clean_mt.isdigit() and len(clean_mt) == 3:
                    mt_list.append(clean_mt.zfill(4))
            mt_list.extend(x for x in _extract_machine_types(model_val) if x not in mt_list)

            ht_match = re.search(r"\b(HT\d{5,})\b", url_val, re.IGNORECASE)
            ht_code = ht_match.group(1).upper() if ht_match else ""
            display_model = model_val or (f"MT {mt_val}" if mt_val else "Lenovo System")
            title = f"Part Removal Video: {display_model}"
            links = [{"label": f"Part Removal Video ({ht_code})" if ht_code else "Part Removal Video", "url": url_val}] if url_val else []

            canon_src = f"video|{display_model.lower()}|{url_val.lower()}"
            canonical_key = hashlib.sha1(canon_src.encode("utf-8")).hexdigest()

            res_text = f"Official Lenovo Part Removal & Replacement Video Guide{' (' + ht_code + ')' if ht_code else ''} for {display_model}."
            if url_val:
                res_text += f" Resource link: {url_val}"

            records.append({
                "canonical_key": canonical_key,
                "source_file": rel_name,
                "sheet_name": sheet_name,
                "row_number": r_idx,
                "source_type": "video_guide",
                "issue_code": ht_code,
                "model_names": display_model,
                "machine_types": ", ".join(mt_list),
                "segment": "",
                "vendor": "Lenovo",
                "commodity": "Part Removal Video",
                "fru_parts": "",
                "buffer_parts": "",
                "pd_code": "",
                "qdts_code": "",
                "critsit": "",
                "applicable_market": "",
                "confirmed_qi": "",
                "issue_status": "Video Guide",
                "impact": "",
                "title": title[:220],
                "symptoms": f"Part removal, teardown, and CRU/FRU replacement video guide for {display_model}",
                "root_cause": "",
                "resolution": res_text,
                "extra_notes": "",
                "links": links,
                "assets": [],
                "record_date": "",
                "quarter": "",
            })
        return records

    def _parse_excel_file(
        self, path: Path, rel_name: str, assets_dir: Path
    ) -> Tuple[List[Dict[str, Any]], int]:
        wb = openpyxl.load_workbook(path, data_only=True)
        all_records: List[Dict[str, Any]] = []
        asset_total = 0
        try:
            for sname in wb.sheetnames:
                ws = wb[sname]
                if getattr(ws, "sheet_state", "visible") != "visible":
                    continue
                if ws.max_row < 2:
                    continue

                source_key = f"{rel_name}::{sname}"
                row_assets, img_cnt = self._extract_ws_images(ws, source_key, assets_dir)
                asset_total += img_cnt

                r1_lower = [_clean_inline(ws.cell(1, c).value).lower() for c in range(1, min(ws.max_column + 1, 20))]
                r2_lower = [_clean_inline(ws.cell(2, c).value).lower() for c in range(1, min(ws.max_column + 1, 15))]

                if "course title" in r2_lower or "course code" in r2_lower or "course title" in r1_lower:
                    all_records.extend(self._parse_thursday_tech_tips_sheet(ws, rel_name, sname))
                elif "product/model" in r1_lower and "issue title" in r1_lower:
                    all_records.extend(self._parse_known_tips_sheet(ws, rel_name, sname, row_assets))
                elif "mt" in r1_lower and "model" in r1_lower and any(k in r1_lower for k in ("hyperlink", "video link", "video", "url")):
                    all_records.extend(self._parse_video_links_sheet(ws, rel_name, sname))
                elif "issues" in r1_lower and ("mt" in r1_lower or "remark" in r1_lower or "root cause" in r1_lower):
                    s_type = "leap_tip" if "leap" in sname.lower() or "leap" in rel_name.lower() else "known_issue"
                    all_records.extend(self._parse_ap_or_leap_sheet(ws, rel_name, sname, s_type, row_assets))
                else:
                    all_records.extend(self._parse_generic_sheet(ws, rel_name, sname, row_assets))
        finally:
            wb.close()
        return all_records, asset_total

    def _parse_csv_file(self, path: Path, rel_name: str) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            for idx, row in enumerate(reader, start=2):
                cleaned = {str(k or "").strip(): _clean_text(v) for k, v in row.items() if v is not None and _clean_text(v)}
                if not cleaned:
                    continue
                combined = " ".join(cleaned.values())
                mts = _extract_machine_types(combined)
                title = next((v for k, v in cleaned.items() if any(w in k.lower() for w in ("issue", "title", "symptom", "topic"))), combined[:180])
                res = next((v for k, v in cleaned.items() if any(w in k.lower() for w in ("resolution", "solution", "remark", "fix"))), " | ".join(f"{k}: {v}" for k, v in cleaned.items()))
                records.append({
                    "canonical_key": hashlib.sha1(f"csv|{rel_name}|{idx}|{title.lower()}".encode("utf-8")).hexdigest(),
                    "source_file": rel_name,
                    "sheet_name": "csv",
                    "row_number": idx,
                    "source_type": "document",
                    "issue_code": next((v for k, v in cleaned.items() if "code" in k.lower() or "id" in k.lower()), ""),
                    "model_names": _extract_model_hints_from_title(combined),
                    "machine_types": ", ".join(mts),
                    "segment": cleaned.get("Segment", ""),
                    "vendor": cleaned.get("Vendor", "Lenovo"),
                    "commodity": "",
                    "fru_parts": "",
                    "buffer_parts": "",
                    "pd_code": "",
                    "qdts_code": "",
                    "critsit": "",
                    "applicable_market": "",
                    "confirmed_qi": "",
                    "issue_status": "",
                    "impact": "",
                    "title": title[:220],
                    "symptoms": combined[:600],
                    "root_cause": "",
                    "resolution": res,
                    "extra_notes": "",
                    "links": [],
                    "assets": [],
                    "record_date": "",
                    "quarter": "",
                })
        return records

    def _chunk_text_records(
        self, text: str, rel_name: str, section_label: str = "Document"
    ) -> List[Dict[str, Any]]:
        clean = _clean_text(text)
        if not clean:
            return []
        chunk_size = 1200
        overlap = 150
        records: List[Dict[str, Any]] = []
        pos = 0
        chunk_idx = 1
        while pos < len(clean):
            piece = clean[pos : pos + chunk_size].strip()
            if piece:
                mts = _extract_machine_types(piece)
                models = _extract_model_hints_from_title(piece)
                first_line = piece.splitlines()[0][:160]
                records.append({
                    "canonical_key": hashlib.sha1(f"doc|{rel_name}|{chunk_idx}|{first_line.lower()}".encode("utf-8")).hexdigest(),
                    "source_file": rel_name,
                    "sheet_name": section_label,
                    "row_number": chunk_idx,
                    "source_type": "document",
                    "issue_code": f"DOC-{chunk_idx:03d}",
                    "model_names": models,
                    "machine_types": ", ".join(mts),
                    "segment": "",
                    "vendor": "Lenovo",
                    "commodity": "",
                    "fru_parts": "",
                    "buffer_parts": "",
                    "pd_code": "",
                    "qdts_code": "",
                    "critsit": "",
                    "applicable_market": "",
                    "confirmed_qi": "",
                    "issue_status": "Reference",
                    "impact": "",
                    "title": f"{Path(rel_name).stem}: {first_line}",
                    "symptoms": piece[:500],
                    "root_cause": "",
                    "resolution": piece,
                    "extra_notes": "",
                    "links": [],
                    "assets": [],
                    "record_date": "",
                    "quarter": "",
                })
                chunk_idx += 1
            if pos + chunk_size >= len(clean):
                break
            pos += chunk_size - overlap
        return records

    def _parse_document_file(self, path: Path, rel_name: str) -> List[Dict[str, Any]]:
        ext = path.suffix.lower()
        if ext == ".pdf":
            try:
                import pypdf
                reader = pypdf.PdfReader(str(path))
                pages = []
                for idx, p in enumerate(reader.pages, start=1):
                    txt = (p.extract_text() or "").strip()
                    if txt:
                        pages.append(f"[Page {idx}]\n{txt}")
                return self._chunk_text_records("\n\n".join(pages), rel_name, "PDF")
            except Exception as exc:
                logger.warning(f"[LenovoKBService] PDF parse error ({rel_name}): {exc}")
                return []
        elif ext == ".docx":
            try:
                import docx
                doc = docx.Document(str(path))
                parts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
                for tbl in doc.tables:
                    for row in tbl.rows:
                        rtxt = " | ".join(c.text.strip() for c in row.cells if c.text.strip())
                        if rtxt:
                            parts.append(rtxt)
                return self._chunk_text_records("\n".join(parts), rel_name, "DOCX")
            except Exception as exc:
                logger.warning(f"[LenovoKBService] DOCX parse error ({rel_name}): {exc}")
                return []
        else:
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
                return self._chunk_text_records(raw, rel_name, ext.lstrip(".").upper())
            except Exception as exc:
                logger.warning(f"[LenovoKBService] Text parse error ({rel_name}): {exc}")
                return []

    def _deduplicate_records(self, raw_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Merges identical records appearing across multiple workbooks/sheets cleanly."""
        merged_map: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []

        for rec in raw_records:
            ckey = rec["canonical_key"]
            src_label = f"{rec['source_file']} ({rec['sheet_name']})"
            if ckey not in merged_map:
                copy_rec = dict(rec)
                copy_rec["source_files_list"] = [src_label]
                merged_map[ckey] = copy_rec
                order.append(ckey)
            else:
                existing = merged_map[ckey]
                if src_label not in existing["source_files_list"]:
                    existing["source_files_list"].append(src_label)
                if rec.get("machine_types"):
                    ex_mts = [t.strip() for t in str(existing.get("machine_types") or "").split(",") if t.strip()]
                    new_mts = [t.strip() for t in str(rec.get("machine_types") or "").split(",") if t.strip()]
                    merged_mts = list(dict.fromkeys(ex_mts + new_mts))
                    existing["machine_types"] = ", ".join(merged_mts)
                # Keep richer fields if new duplicate fills any missing metadata
                for fld in (
                    "issue_code", "model_names", "machine_types", "segment", "vendor",
                    "commodity", "fru_parts", "buffer_parts", "pd_code", "qdts_code",
                    "confirmed_qi", "issue_status", "impact", "root_cause", "resolution",
                    "extra_notes", "record_date", "quarter",
                ):
                    if not existing.get(fld) and rec.get(fld):
                        existing[fld] = rec[fld]
                for lk in rec.get("links", []):
                    if lk not in existing["links"]:
                        existing["links"].append(lk)
                for ast in rec.get("assets", []):
                    if ast not in existing["assets"]:
                        existing["assets"].append(ast)

        return [merged_map[k] for k in order]

    def sync_knowledge_base(
        self,
        force: bool = False,
        kb_dir: Optional[Path] = None,
        assets_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """
        Synchronizes `Tips & Resources` files into SQLite WAL + FTS5 index.
        Fast no-op (<2ms) when mtime and file sizes match indexed state unless force=True.
        """
        self.ensure_tables()
        root = self._get_kb_dir(kb_dir)
        assets_root = self._get_assets_dir(assets_dir)

        with self._sync_lock:
            files = self._list_source_files(root)
            existing_rows = db._fetchall_sync("SELECT * FROM kb_sources")
            existing_by_name = {r["file_name"]: r for r in existing_rows}

            current_meta: Dict[str, Tuple[Path, float, int]] = {}
            for fp in files:
                rel_name = str(fp.relative_to(root)).replace("\\", "/")
                st = fp.stat()
                current_meta[rel_name] = (fp, float(st.st_mtime), int(st.st_size))

            if not force and set(current_meta.keys()) == set(existing_by_name.keys()):
                all_match = True
                for rel_name, (_, mtime_val, size_val) in current_meta.items():
                    ex = existing_by_name[rel_name]
                    if abs(float(ex.get("mtime") or 0.0) - mtime_val) > 0.01 or int(ex.get("file_size") or -1) != size_val:
                        all_match = False
                        break
                if all_match:
                    cnt_row = db._fetchone_sync("SELECT COUNT(*) AS cnt FROM kb_entries")
                    return {
                        "synced": False,
                        "reason": "up_to_date",
                        "sources_count": len(current_meta),
                        "entries_count": int((cnt_row or {}).get("cnt") or 0),
                    }

            # Clean assets dir on full rebuild so removed files leave zero orphan images
            if assets_root.exists():
                for child in assets_root.iterdir():
                    try:
                        if child.is_dir():
                            shutil.rmtree(child, ignore_errors=True)
                        else:
                            child.unlink(missing_ok=True)
                    except Exception:
                        pass

            raw_records: List[Dict[str, Any]] = []
            source_stats: List[Tuple[Any, ...]] = []
            now_iso = datetime.datetime.now().isoformat()

            for rel_name, (fp, mtime_val, size_val) in current_meta.items():
                sha = _sha256_file(fp)
                ext = fp.suffix.lower()
                rec_cnt = 0
                asset_cnt = 0
                status = "indexed"
                err_msg = ""
                try:
                    if ext in (".xlsx", ".xls"):
                        recs, asset_cnt = self._parse_excel_file(fp, rel_name, assets_root)
                    elif ext == ".csv":
                        recs = self._parse_csv_file(fp, rel_name)
                    else:
                        recs = self._parse_document_file(fp, rel_name)
                    rec_cnt = len(recs)
                    raw_records.extend(recs)
                except Exception as exc:
                    status = "error"
                    err_msg = str(exc)[:300]
                    logger.warning(f"[LenovoKBService] Failed parsing {rel_name}: {exc}")

                source_stats.append((
                    rel_name,
                    str(fp.resolve()),
                    sha,
                    size_val,
                    mtime_val,
                    rec_cnt,
                    asset_cnt,
                    status,
                    err_msg,
                    now_iso,
                ))

            deduped = self._deduplicate_records(raw_records)

            entry_rows: List[Tuple[Any, ...]] = []
            fts_rows: List[Tuple[Any, ...]] = []

            for item in deduped:
                entry_id = str(uuid.uuid4())
                codes_text = " ".join(
                    x for x in [
                        item.get("issue_code", ""),
                        item.get("pd_code", ""),
                        item.get("qdts_code", ""),
                        item.get("critsit", ""),
                        item.get("extra_notes", ""),
                    ] if x
                )
                raw_blob = " ".join(
                    x for x in [
                        item.get("model_names", ""),
                        item.get("machine_types", ""),
                        item.get("segment", ""),
                        item.get("vendor", ""),
                        item.get("commodity", ""),
                        item.get("title", ""),
                        item.get("symptoms", ""),
                        item.get("root_cause", ""),
                        item.get("resolution", ""),
                        item.get("fru_parts", ""),
                        item.get("buffer_parts", ""),
                        codes_text,
                    ] if x
                )
                expanded_blob = _expand_text_aliases(raw_blob)

                entry_rows.append((
                    entry_id,
                    item["canonical_key"],
                    item["source_file"],
                    json.dumps(item.get("source_files_list", [item["source_file"]])),
                    item.get("sheet_name", ""),
                    int(item.get("row_number") or 0),
                    item.get("source_type", "known_tip"),
                    item.get("issue_code", ""),
                    item.get("model_names", ""),
                    item.get("machine_types", ""),
                    item.get("segment", ""),
                    item.get("vendor", ""),
                    item.get("commodity", ""),
                    item.get("fru_parts", ""),
                    item.get("buffer_parts", ""),
                    item.get("pd_code", ""),
                    item.get("qdts_code", ""),
                    item.get("critsit", ""),
                    item.get("applicable_market", ""),
                    item.get("confirmed_qi", ""),
                    item.get("issue_status", ""),
                    item.get("impact", ""),
                    item.get("title", ""),
                    item.get("symptoms", ""),
                    item.get("root_cause", ""),
                    item.get("resolution", ""),
                    item.get("extra_notes", ""),
                    json.dumps(item.get("links", [])),
                    json.dumps(item.get("assets", [])),
                    item.get("record_date", ""),
                    item.get("quarter", ""),
                    expanded_blob[:6000],
                    now_iso,
                ))

                fts_rows.append((
                    entry_id,
                    _expand_text_aliases(item.get("title", "")),
                    _expand_text_aliases(item.get("model_names", "")),
                    item.get("machine_types", ""),
                    item.get("symptoms", ""),
                    item.get("root_cause", ""),
                    item.get("resolution", ""),
                    item.get("fru_parts", ""),
                    codes_text,
                    expanded_blob[:6000],
                ))

            # Commit atomically via connection
            with db._lock:
                conn = db._get_connection()
                try:
                    conn.execute("DELETE FROM kb_entries_fts")
                    conn.execute("DELETE FROM kb_entries")
                    conn.execute("DELETE FROM kb_sources")
                    conn.executemany(
                        """
                        INSERT INTO kb_sources
                        (file_name, file_path, sha256, file_size, mtime, record_count, asset_count, status, error_msg, last_synced_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        source_stats,
                    )
                    conn.executemany(
                        """
                        INSERT INTO kb_entries (
                            id, canonical_key, source_file, source_files_json, sheet_name, row_number,
                            source_type, issue_code, model_names, machine_types, segment, vendor,
                            commodity, fru_parts, buffer_parts, pd_code, qdts_code, critsit,
                            applicable_market, confirmed_qi, issue_status, impact, title, symptoms,
                            root_cause, resolution, extra_notes, links_json, assets_json,
                            record_date, quarter, search_blob, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        entry_rows,
                    )
                    conn.executemany(
                        """
                        INSERT INTO kb_entries_fts (
                            entry_id, title, model_names, machine_types, symptoms,
                            root_cause, resolution, fru_parts, codes, search_blob
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        fts_rows,
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

            self._refresh_known_mt_cache()
            self._last_check_ts = time.time()
            return {
                "synced": True,
                "reason": "rebuilt",
                "sources_count": len(source_stats),
                "raw_records_count": len(raw_records),
                "entries_count": len(entry_rows),
                "deduplicated_removed": len(raw_records) - len(entry_rows),
            }

    def ensure_synced(self, max_staleness_seconds: float = 15.0) -> None:
        """Fast throttled incremental check so newly dropped/updated files auto-index."""
        self.ensure_tables()
        now = time.time()
        cur_path = str(db.db_path)
        if self._cache_db_path == cur_path and (now - self._last_check_ts) < max_staleness_seconds:
            return
        self._last_check_ts = now
        try:
            self.sync_knowledge_base(force=False)
        except Exception as exc:
            logger.warning(f"[LenovoKBService] Auto-sync notice: {exc}")

    def detect_lenovo_intent(self, query: str) -> bool:
        """
        Detects whether a user query is asking about Lenovo models, Machine Types (MT),
        known hardware/driver issues, FRU/PD/APTT codes, or troubleshooting tips.
        Avoids false-positive triggers on general programming/non-Lenovo prompts.
        """
        if not query or not query.strip():
            return False
        q = query.strip()
        ql = q.lower()

        if ql.startswith(("/tips", "/kb", "!tips", "!kb")):
            return True

        brand_terms = (
            "lenovo", "thinkpad", "thinkbook", "thinkcentre", "thinkvision", "thinkstation",
            "ideapad", "ideacentre", "yoga", "legion", "loq", "vantage",
            "leap tip", "thursday tech", "known tip", "known issue",
            "part removal", "removal video", "replacement video", "removal guide",
            "disassembly", "teardown", "fru video", "cru video",
            "qdts", "critsit", "confirmed qi", "impactfru", "buffer part",
            "pd code", "pdt", "aptt", "mbd", "bezel peel", "hinge",
        )
        if any(bt in ql for bt in brand_terms):
            return True

        # Exact Lenovo model series patterns
        model_patterns = [
            r"\bt(?:14|14s|15|16|490|490s|495|495s)\b",
            r"\bl(?:13|14|15|390|580|590)\b",
            r"\bx(?:1\s*carbon|1c|1\s*yoga|390|395)\b",
            r"\be(?:14|15)\s*(?:gen|g\d)?\b",
            r"\bm(?:90a|90q|70|80)\b",
            r"\bp(?:27h|34w|14|15|16)\b",
            r"\b(?:slim|ip|ip3|ip5)\s*[35]\b",
            r"\b(?:flex|essential)\s*\d+\b",
            r"\b(?:battery|keyboard|ssd|hdd|hard\s*disk|memory|ram|lcd|screen|panel|base\s*cover|bottom\s*case|touchpad|fan|thermal|heatsink|wifi|wireless|speaker|motherboard|system\s*board|hinge|bezel)\s+(?:removal|replacement|teardown|disassembly)\b",
            r"\b(?:14|15|16)(?:iah|itl|irh|arp|akp|ahp|iau|alc|iap|iax|irx)\d*\b",
            r"\b(?:aptt|qdts|mcnw|mtpw|rcnw|ctpi)\d+[a-z0-9]*\b",
            r"\bht\d{4,}\b",
            r"\b5[a-z]\d{2}[a-z0-9]{5,}\b",  # FRU numbers like 5B11Q02019
            r"\bpost\s*210[01]\b|\bno\s+post\b|\bbezel\s+peel\b|\bnovo\s+button\b|\b0xc0000135\b|\btrackpoint\b",
        ]
        if any(re.search(pat, q, re.IGNORECASE) for pat in model_patterns):
            return True

        # Check against actual indexed Machine Types (3-6 chars like 21CB, 20S0, 0302, 10050, Z380) in SQLite
        self.ensure_synced()
        if not self._known_mt_cache:
            self._refresh_known_mt_cache()
        tokens = set(re.findall(r"\b[A-Z0-9]{3,6}\b", q.upper()))
        if tokens & self._known_mt_cache:
            return True

        return False

    def _tokenize_query(self, query: str) -> Tuple[List[str], List[str], List[str]]:
        """Returns (expanded_terms, exact_codes, mt_tokens)."""
        clean_q = re.sub(r"^(?:/tips|/kb|!tips|!kb|/search|!search)\s+", "", query.strip(), flags=re.IGNORECASE)
        mts = _extract_machine_types(clean_q)
        raw_tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-\_\/\.]{1,}", clean_q)

        exact_codes: List[str] = []
        search_terms: List[str] = []
        seen: Set[str] = set()

        for tok in raw_tokens:
            tl = tok.lower().strip("-_/.")
            if not tl or tl in _STOPWORDS:
                continue
            if re.search(r"\d", tok) and len(tok) >= 3:
                if tok.upper() not in exact_codes:
                    exact_codes.append(tok.upper())
            if tl not in seen:
                seen.add(tl)
                search_terms.append(tl)
            for exp in _REVERSE_ALIAS_TOKENS.get(tl, []):
                if exp not in seen:
                    seen.add(exp)
                    search_terms.append(exp)
            # Also expand compact forms like Gen2 -> Gen, 2
            m_gen = re.match(r"^(?:gen|g)(\d+)$", tl)
            if m_gen:
                num = m_gen.group(1)
                for g_tok in ("gen", num, f"g{num}", f"gen{num}"):
                    if g_tok not in seen:
                        seen.add(g_tok)
                        search_terms.append(g_tok)

        return search_terms, exact_codes, mts

    def _row_to_dict(self, row: Dict[str, Any], score: float = 0.0) -> Dict[str, Any]:
        def parse_json_list(raw: Any) -> List[Any]:
            if isinstance(raw, list):
                return raw
            try:
                val = json.loads(raw or "[]")
                return val if isinstance(val, list) else []
            except Exception:
                return []

        return {
            "id": row["id"],
            "source_file": row["source_file"],
            "source_files": parse_json_list(row.get("source_files_json")),
            "sheet_name": row.get("sheet_name", ""),
            "row_number": int(row.get("row_number") or 0),
            "source_type": row.get("source_type", "known_tip"),
            "source_label": SOURCE_TYPE_LABELS.get(row.get("source_type", ""), "📄 Resource"),
            "issue_code": row.get("issue_code", ""),
            "model_names": row.get("model_names", ""),
            "machine_types": row.get("machine_types", ""),
            "segment": row.get("segment", ""),
            "vendor": row.get("vendor", ""),
            "commodity": row.get("commodity", ""),
            "fru_parts": row.get("fru_parts", ""),
            "buffer_parts": row.get("buffer_parts", ""),
            "pd_code": row.get("pd_code", ""),
            "qdts_code": row.get("qdts_code", ""),
            "critsit": row.get("critsit", ""),
            "applicable_market": row.get("applicable_market", ""),
            "confirmed_qi": row.get("confirmed_qi", ""),
            "issue_status": row.get("issue_status", ""),
            "impact": row.get("impact", ""),
            "title": row.get("title", ""),
            "symptoms": row.get("symptoms", ""),
            "root_cause": row.get("root_cause", ""),
            "resolution": row.get("resolution", ""),
            "extra_notes": row.get("extra_notes", ""),
            "links": parse_json_list(row.get("links_json")),
            "assets": parse_json_list(row.get("assets_json")),
            "record_date": row.get("record_date", ""),
            "quarter": row.get("quarter", ""),
            "score": round(score, 2),
        }

    def search(
        self,
        query: str,
        limit: int = 8,
        source_type: Optional[str] = None,
        segment: Optional[str] = None,
        confirmed_only: bool = False,
        min_score: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """
        Hybrid SQLite FTS5 BM25 + Exact MT/FRU/PD/Model Token search across Tips & Resources.
        Automatically triggers lightweight incremental sync if files changed on disk.
        """
        self.ensure_synced()

        clean_query = re.sub(r"^(?:/tips|/kb|!tips|!kb|/search|!search)\s+", "", (query or "").strip(), flags=re.IGNORECASE)
        terms, exact_codes, mts = self._tokenize_query(clean_query)

        filter_clauses = []
        filter_params: List[Any] = []
        if source_type and source_type != "all":
            filter_clauses.append("e.source_type = ?")
            filter_params.append(source_type)
        if segment and segment != "all":
            filter_clauses.append("LOWER(e.segment) = ?")
            filter_params.append(segment.strip().lower())
        if confirmed_only:
            filter_clauses.append("LOWER(e.confirmed_qi) = 'yes'")

        where_extra = (" AND " + " AND ".join(filter_clauses)) if filter_clauses else ""

        # If query is empty (browsing Explorer), return latest prioritized records matching filters
        if not clean_query:
            sql = (
                "SELECT e.* FROM kb_entries e "
                "WHERE 1=1" + where_extra +
                " ORDER BY CASE WHEN e.source_type IN ('known_issue','known_tip','leap_tip') THEN 0 ELSE 1 END, "
                "e.record_date DESC, e.row_number ASC LIMIT ?"
            )
            rows = db._fetchall_sync(sql, tuple(filter_params + [limit]))
            return [self._row_to_dict(r, score=10.0) for r in rows]

        candidates: Dict[str, Tuple[Dict[str, Any], float]] = {}

        # 1. FTS5 BM25 Retrieval
        if terms:
            fts_tokens = []
            for t in terms[:14]:
                safe_t = re.sub(r"[^A-Za-z0-9]", "", t)
                if len(safe_t) >= 2:
                    fts_tokens.append(f'"{safe_t}"*')
            if fts_tokens:
                fts_match = " OR ".join(dict.fromkeys(fts_tokens))
                try:
                    fts_sql = (
                        "SELECT e.*, bm25(kb_entries_fts, 0.0, 4.0, 6.0, 7.0, 3.0, 2.0, 4.0, 5.0, 6.0, 1.0) AS rank_score "
                        "FROM kb_entries_fts f "
                        "JOIN kb_entries e ON f.entry_id = e.id "
                        f"WHERE kb_entries_fts MATCH ?{where_extra} "
                        "ORDER BY rank_score ASC LIMIT ?"
                    )
                    fts_rows = db._fetchall_sync(fts_sql, tuple([fts_match] + filter_params + [max(40, limit * 5)]))
                    for r in fts_rows:
                        raw_bm25 = abs(float(r.get("rank_score") or 0.0))
                        candidates[r["id"]] = (r, min(40.0, raw_bm25 * 2.5))
                except Exception as exc:
                    logger.debug(f"[LenovoKBService] FTS query notice: {exc}")

        # 2. Exact code / model token LIKE sweep (catches hyphenated SKUs & 4-char MTs)
        like_tokens = list(dict.fromkeys(mts + exact_codes + [t.upper() for t in terms if len(t) >= 4]))[:8]
        if like_tokens:
            like_clauses = " OR ".join("UPPER(e.search_blob) LIKE ?" for _ in like_tokens)
            like_params = [f"%{tk}%" for tk in like_tokens]
            like_sql = (
                f"SELECT e.* FROM kb_entries e WHERE ({like_clauses}){where_extra} LIMIT ?"
            )
            try:
                like_rows = db._fetchall_sync(like_sql, tuple(like_params + filter_params + [max(40, limit * 5)]))
                for r in like_rows:
                    if r["id"] not in candidates:
                        candidates[r["id"]] = (r, 5.0)
            except Exception as exc:
                logger.debug(f"[LenovoKBService] LIKE sweep notice: {exc}")

        # 3. Domain-Specific Hybrid Re-Ranking
        scored_results: List[Dict[str, Any]] = []
        phrase_l = clean_query.lower()
        meaningful_terms = [t for t in terms if len(t) >= 2]
        raw_words = [w.lower() for w in re.findall(r"[A-Za-z0-9\-]+", clean_query)]
        bigrams = [f"{raw_words[i]} {raw_words[i + 1]}" for i in range(len(raw_words) - 1)]
        req_gens = set(re.findall(r"\bgen\s*(\d+)\b|\bg(\d+)\b", phrase_l))
        req_gen_nums = {g for tup in req_gens for g in tup if g}
        family_aliases: List[str] = []
        if re.search(r"\bideapad\s*3\b|\bip\s*3\b|\bip3\b", phrase_l):
            family_aliases.extend(["ip3", "ip 3", "ideapad 3"])
        if re.search(r"\bideapad\s*5\b|\bip\s*5\b|\bip5\b", phrase_l):
            family_aliases.extend(["ip5", "ip 5", "ideapad 5"])
        if re.search(r"\bideapad\s*slim\s*3\b|\bslim\s*3\b", phrase_l):
            family_aliases.extend(["slim 3", "ips3"])
        if re.search(r"\bideapad\s*slim\s*5\b|\bslim\s*5\b", phrase_l):
            family_aliases.extend(["slim 5", "ips5"])

        is_video_intent = bool(re.search(r"\b(video|videos|removal|remove|disassembl\w*|teardown|replace\w*|cru)\b", phrase_l))

        for _, (row, base_bm25) in candidates.items():
            score = base_bm25
            title_l = (row.get("title") or "").lower()
            model_l = (row.get("model_names") or "").lower()
            mt_u = (row.get("machine_types") or "").upper()
            symp_l = (row.get("symptoms") or "").lower()
            rc_l = (row.get("root_cause") or "").lower()
            res_l = (row.get("resolution") or "").lower()
            fru_u = (row.get("fru_parts") or "").upper()
            codes_u = " ".join([
                str(row.get("issue_code") or ""),
                str(row.get("pd_code") or ""),
                str(row.get("qdts_code") or ""),
                str(row.get("extra_notes") or ""),
            ]).upper()
            blob_l = (row.get("search_blob") or "").lower()

            # Exact MT hit (+65 per MT)
            for mt in mts:
                if re.search(rf"\b{re.escape(mt)}\b", mt_u) or re.search(rf"\b{re.escape(mt)}\b", codes_u):
                    score += 65.0

            # Exact code / numeric MT / SKU / APTT / FRU / PD hit (+45 to +58)
            for ec in exact_codes:
                if re.search(rf"\b{re.escape(ec)}\b", mt_u):
                    score += 58.0
                elif ec in mt_u or ec in codes_u or ec in fru_u:
                    score += 45.0
                elif ec.lower() in model_l or ec.lower() in title_l:
                    score += 38.0
                elif ec.lower() in blob_l:
                    score += 18.0

            # Term coverage ratio across title / models / resolution
            if meaningful_terms:
                matched_cnt = 0
                for t in meaningful_terms:
                    if t in model_l or t in title_l:
                        score += 14.0
                        if re.search(rf"\b{re.escape(t)}\b", f"{title_l} {model_l}"):
                            score += 12.0
                        matched_cnt += 1
                    elif t in symp_l or t in res_l or t in rc_l:
                        score += 7.0
                        matched_cnt += 1
                    elif t in blob_l:
                        score += 3.0
                        matched_cnt += 1
                coverage = matched_cnt / max(1, len(meaningful_terms))
                score += coverage * 20.0

            # Bigram & family phrase matching + generation specificity
            hdr_text = f"{title_l} {model_l}"
            for bg in bigrams:
                if len(bg) >= 4 and bg in hdr_text:
                    score += 18.0
            if family_aliases and any(fa in hdr_text for fa in family_aliases):
                score += 40.0
            if req_gen_nums:
                row_gens = {g for tup in re.findall(r"\bgen\s*(\d+)\b|\bg(\d+)\b", hdr_text) for g in tup if g}
                if req_gen_nums & row_gens:
                    score += 25.0
                elif row_gens and not (req_gen_nums & row_gens):
                    score -= 22.0

            # Exact multi-word phrase match boost
            if len(phrase_l) >= 5:
                if phrase_l in title_l or phrase_l in model_l:
                    score += 35.0
                elif phrase_l in symp_l or phrase_l in res_l:
                    score += 20.0

            # Actionability & quality boosts
            if is_video_intent and row.get("source_type") == "video_guide":
                score += 38.0
            elif row.get("source_type") in ("known_issue", "known_tip", "leap_tip") and len(res_l) > 15:
                score += 12.0
            if (row.get("confirmed_qi") or "").strip().lower() == "yes":
                score += 8.0
            if row.get("fru_parts") or row.get("pd_code") or row.get("buffer_parts"):
                score += 4.0

            rec_date = row.get("record_date") or ""
            if rec_date.startswith(("2025", "2026")):
                score += 4.0
            elif rec_date.startswith("2024"):
                score += 2.0

            if score >= min_score:
                scored_results.append(self._row_to_dict(row, score=score))

        scored_results.sort(key=lambda x: (-x["score"], x.get("source_type") == "tech_tip", -(len(x.get("resolution") or ""))))
        return scored_results[:limit]

    def format_context_for_llm(self, query: str, matches: List[Dict[str, Any]], max_items: int = 6) -> str:
        """Builds a compact, citation-rich context block with strict anti-hallucination rules."""
        if not matches:
            return ""
        selected = matches[:max_items]
        lines = [
            f"### 📚 Verified Lenovo 'Tips & Resources' Knowledge Base Matches for: '{query}'",
            "Use the official internal Lenovo service records below to answer the user's question accurately.\n",
        ]
        for idx, item in enumerate(selected, start=1):
            hdr = f"[KB-{idx}] **{item.get('title', 'Untitled')}** ({item.get('source_label', '')} • Source: `{item.get('source_file', '')}`)"
            meta_bits = []
            if item.get("issue_code"):
                meta_bits.append(f"ID/Code: `{item['issue_code']}`")
            if item.get("model_names"):
                meta_bits.append(f"Models: {item['model_names'][:180]}")
            if item.get("machine_types"):
                meta_bits.append(f"MT: `{item['machine_types']}`")
            if item.get("confirmed_qi"):
                meta_bits.append(f"Confirmed QI: {item['confirmed_qi']}")
            if item.get("issue_status"):
                meta_bits.append(f"Status: {item['issue_status']}")
            if item.get("segment"):
                meta_bits.append(f"Segment: {item['segment']}")

            lines.append(hdr)
            if meta_bits:
                lines.append("- Metadata: " + " | ".join(meta_bits))
            if item.get("symptoms") and item["symptoms"] != item.get("title"):
                lines.append(f"- Symptom / Description: {item['symptoms'][:380]}")
            if item.get("root_cause"):
                lines.append(f"- Root Cause: {item['root_cause'][:280]}")
            if item.get("resolution"):
                lines.append(f"- Verified Resolution / Tip: {item['resolution'][:520]}")

            part_bits = []
            if item.get("fru_parts"):
                part_bits.append(f"ImpactFRU: `{item['fru_parts']}`")
            if item.get("buffer_parts"):
                part_bits.append(f"Buffer Parts: `{item['buffer_parts']}`")
            if item.get("pd_code"):
                part_bits.append(f"PD Code: `{item['pd_code']}`")
            if item.get("qdts_code"):
                part_bits.append(f"QDTS: `{item['qdts_code']}`")
            if part_bits:
                lines.append("- Parts & Codes: " + " | ".join(part_bits))

            if item.get("links"):
                link_strs = [f"[{lk.get('label', 'Link')}]({lk['url']})" for lk in item["links"][:3] if lk.get("url")]
                if link_strs:
                    lines.append("- Official Links: " + " • ".join(link_strs))
            if item.get("assets"):
                lines.append(f"- Embedded SOP Screenshots Attached: {len(item['assets'])} image(s) displayed to user.")
            lines.append("")

        lines.append(
            "**Strict Grounding Rules for AI:**\n"
            "- Prioritize the verified `[KB-n]` records above over general training memory.\n"
            "- Explicitly mention matching Models, MT codes, Root Cause, Recommended Resolution, and any FRU / Buffer Parts / PD / QDTS codes found above.\n"
            "- Include clickable links from `Official Links` when available.\n"
            "- NEVER invent FRU part numbers, BIOS versions, MT codes, or URLs not shown in `[KB-n]`."
        )
        return "\n".join(lines)

    def get_stats(self) -> Dict[str, Any]:
        """Returns source file breakdown, entry totals, and facet values for the Explorer & Admin UI."""
        self.ensure_synced()
        sources = db._fetchall_sync(
            "SELECT file_name, file_size, record_count, asset_count, status, error_msg, last_synced_at "
            "FROM kb_sources ORDER BY file_name ASC"
        )
        type_rows = db._fetchall_sync(
            "SELECT source_type, COUNT(*) AS cnt FROM kb_entries GROUP BY source_type"
        )
        seg_rows = db._fetchall_sync(
            "SELECT DISTINCT segment FROM kb_entries WHERE segment != '' ORDER BY segment ASC"
        )
        total_row = db._fetchone_sync("SELECT COUNT(*) AS cnt FROM kb_entries")
        qi_row = db._fetchone_sync("SELECT COUNT(*) AS cnt FROM kb_entries WHERE LOWER(confirmed_qi) = 'yes'")
        return {
            "kb_dir": str(self._get_kb_dir()),
            "total_entries": int((total_row or {}).get("cnt") or 0),
            "confirmed_qi_count": int((qi_row or {}).get("cnt") or 0),
            "sources": sources,
            "by_type": {r["source_type"]: int(r["cnt"]) for r in type_rows},
            "segments": [r["segment"] for r in seg_rows],
            "last_synced_at": max((s.get("last_synced_at") or "" for s in sources), default=""),
        }

    def save_uploaded_resource(
        self,
        filename: str,
        file_bytes: bytes,
        kb_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Saves a validated workbook/document into Tips & Resources and triggers immediate re-sync."""
        clean_name = Path(str(filename or "")).name
        if not clean_name or clean_name.startswith("."):
            raise ValueError("Invalid filename.")
        ext = Path(clean_name).suffix.lower()
        if ext not in SUPPORTED_KB_EXTENSIONS:
            raise ValueError(
                f"Unsupported file format '{ext}'. Supported formats: {', '.join(sorted(SUPPORTED_KB_EXTENSIONS))}"
            )
        if not file_bytes:
            raise ValueError("Uploaded file is empty.")

        target_dir = self._get_kb_dir(kb_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        dest_path = (target_dir / clean_name).resolve()
        if dest_path.parent != target_dir.resolve():
            raise ValueError("Path traversal blocked.")

        dest_path.write_bytes(file_bytes)
        sync_res = self.sync_knowledge_base(force=True, kb_dir=target_dir)
        return {
            "saved_file": clean_name,
            "file_size": len(file_bytes),
            "sync_result": sync_res,
        }

    def resolve_solution_videos(
        self,
        url_or_id: str,
        query_hint: str = "",
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """Resolves Lenovo support solution URLs into playable MP4/YouTube part removal videos and HMM manuals."""
        raw = str(url_or_id or "").strip()
        if not raw:
            return {
                "solution_id": "",
                "original_url": "",
                "resolved_url": "",
                "cdn_script_url": "",
                "page_title": "",
                "videos": [],
                "recommended_video": None,
                "best_index": 0,
                "manuals": [],
                "error": "No URL or solution ID provided.",
                "cached": False,
            }

        # Direct playable MP4 / YouTube links require no scraping
        if _is_direct_video_url(raw):
            vtype = _classify_video_url(raw)
            title = _title_from_video_url(raw)
            vid = {
                "title": title,
                "url": raw,
                "video_type": vtype,
                "action_type": _infer_video_action(title, raw),
                "match_score": 1.0,
            }
            return {
                "solution_id": "",
                "original_url": raw,
                "resolved_url": raw,
                "cdn_script_url": "",
                "page_title": title,
                "videos": [vid],
                "recommended_video": vid,
                "best_index": 0,
                "manuals": [],
                "error": "",
                "cached": False,
            }

        ht_id, canonical_url = _normalize_lenovo_solution_url(raw)
        cache_key = canonical_url or raw

        cached_payload: Optional[Dict[str, Any]] = None
        if not force_refresh and cache_key in self._video_cache:
            entry, ts = self._video_cache[cache_key]
            if (time.monotonic() - ts) < self._video_cache_ttl:
                cached_payload = entry

        was_cached = cached_payload is not None
        if cached_payload is None:
            cached_payload = self._fetch_lenovo_solution_media(raw, ht_id, canonical_url)
            if cached_payload.get("videos") or not cached_payload.get("error"):
                self._video_cache[cache_key] = (cached_payload, time.monotonic())

        videos = [dict(v) for v in (cached_payload.get("videos") or [])]
        best_idx = 0
        recommended = None
        if videos:
            best_score = float("-inf")
            for idx, vid in enumerate(videos):
                sc = _score_video_match(vid, query_hint)
                vid["match_score"] = round(sc, 2)
                if sc > best_score:
                    best_score = sc
                    best_idx = idx
            recommended = dict(videos[best_idx])

        return {
            "solution_id": cached_payload.get("solution_id", ht_id),
            "original_url": raw,
            "resolved_url": cached_payload.get("resolved_url", canonical_url),
            "cdn_script_url": cached_payload.get("cdn_script_url", ""),
            "page_title": cached_payload.get("page_title", ""),
            "videos": videos,
            "recommended_video": recommended,
            "best_index": best_idx,
            "manuals": [dict(m) for m in (cached_payload.get("manuals") or [])],
            "error": cached_payload.get("error", ""),
            "cached": was_cached,
        }

    def _fetch_lenovo_solution_media(
        self, original_url: str, ht_id: str, resolved_url: str
    ) -> Dict[str, Any]:
        target_url = resolved_url or original_url
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        cdn_script_url = ""
        page_title = ""
        body_html = ""
        html_text = ""

        try:
            with httpx.Client(follow_redirects=True, timeout=12.0, headers=headers) as client:
                resp = client.get(target_url)
                if resp.status_code >= 400:
                    return {
                        "solution_id": ht_id,
                        "original_url": original_url,
                        "resolved_url": str(resp.url or target_url),
                        "cdn_script_url": "",
                        "page_title": "",
                        "videos": [],
                        "manuals": [],
                        "error": f"Lenovo support page returned HTTP {resp.status_code}.",
                    }
                target_url = str(resp.url or target_url)
                html_text = resp.text or ""
                title_m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.I | re.S)
                if title_m:
                    page_title = re.sub(r"\s+", " ", html_lib_unescape(title_m.group(1))).strip()

                cdn_matches = re.findall(
                    r'((?:https?://pcsupport\.lenovo\.com)?/(?:[a-z]{2}/[a-z]{2}/)?api/v4/contents/cdn/[a-zA-Z0-9_]+\.js)',
                    html_text,
                )
                for cdn_path in cdn_matches[:2]:
                    full_cdn = (
                        cdn_path
                        if cdn_path.startswith("http")
                        else f"https://pcsupport.lenovo.com{cdn_path}"
                    )
                    cdn_resp = client.get(full_cdn)
                    if cdn_resp.status_code < 400 and cdn_resp.text:
                        cdn_script_url = full_cdn
                        cdn_data = _parse_lenovo_cdn_payload(cdn_resp.text)
                        if cdn_data.get("body"):
                            body_html = cdn_data["body"]
                        else:
                            body_html = cdn_resp.text
                        if not page_title and cdn_data.get("title"):
                            page_title = str(cdn_data["title"]).strip()
                        if body_html:
                            break
        except Exception as exc:
            logger.warning(f"[LenovoKBService] resolve_solution_videos failed for {target_url}: {exc}")
            return {
                "solution_id": ht_id,
                "original_url": original_url,
                "resolved_url": target_url,
                "cdn_script_url": cdn_script_url,
                "page_title": page_title,
                "videos": [],
                "manuals": [],
                "error": f"Could not reach pcsupport.lenovo.com ({exc}).",
            }

        videos, manuals = _extract_media_from_html(body_html or html_text)
        if not videos and html_text and body_html:
            # Fallback scan on outer page HTML in case video embed lives outside CDN body
            videos, extra_manuals = _extract_media_from_html(html_text)
            if not manuals:
                manuals = extra_manuals

        return {
            "solution_id": ht_id,
            "original_url": original_url,
            "resolved_url": target_url,
            "cdn_script_url": cdn_script_url,
            "page_title": page_title,
            "videos": videos,
            "manuals": manuals,
            "error": "" if videos else "No direct playable video links were found on the solution page.",
        }


def html_lib_unescape(text: str) -> str:
    import html as _html
    return _html.unescape(text or "")


_VIDEO_FILE_RE = re.compile(r"\.(?:mp4|webm|m4v|mov|m3u8)(?:$|\?)", re.I)
_YOUTUBE_VIDEO_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?[^#]*v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{6,})",
    re.I,
)
_HT_ID_RE = re.compile(r"\b(HT\d{4,10})\b", re.I)

_PART_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "ssd": ("ssd", "solid state", "m.2", "nvme", "hard disk", "hdd", "storage"),
    "hdd": ("hard disk", "hdd", "ssd", "storage", "drive"),
    "ram": ("memory", "ram", "dimm", "sodimm"),
    "memory": ("memory", "ram", "dimm", "sodimm"),
    "battery": ("battery", "built-in battery"),
    "cmos": ("coin cell", "cmos", "rtc"),
    "coin": ("coin cell", "cmos", "rtc"),
    "keyboard": ("keyboard", "kbd", "key"),
    "screen": ("lcd", "display", "panel", "screen", "bezel"),
    "lcd": ("lcd", "display", "panel", "screen", "bezel"),
    "display": ("lcd", "display", "panel", "screen"),
    "base": ("base cover", "bottom case", "bottom", "cover", "door"),
    "bottom": ("bottom case", "base cover", "bottom", "cover", "door"),
    "cover": ("base cover", "bottom case", "cover", "door"),
    "fan": ("fan", "thermal", "heatsink", "cooling"),
    "thermal": ("thermal", "heatsink", "fan"),
    "heatsink": ("heatsink", "thermal", "fan"),
    "wifi": ("wireless", "wlan", "wifi", "wi-fi", "antenna"),
    "wlan": ("wireless", "wlan", "wifi", "antenna"),
    "wireless": ("wireless", "wlan", "wifi"),
    "touchpad": ("touchpad", "trackpad", "clickpad"),
    "trackpad": ("touchpad", "trackpad", "clickpad"),
    "speaker": ("speaker", "speakers", "audio"),
    "motherboard": ("system board", "motherboard", "mainboard"),
    "board": ("system board", "motherboard", "power button board", "io board"),
    "hinge": ("hinge", "hinges"),
    "camera": ("camera", "webcam", "ir camera"),
}

_STOP_HINT_TOKENS = {
    "lenovo", "thinkpad", "ideapad", "thinkbook", "thinkcentre", "thinkstation",
    "yoga", "legion", "loq", "flex", "essential", "ideacentre", "video", "videos",
    "link", "links", "remove", "removal", "replace", "replacement", "how", "to",
    "part", "parts", "show", "play", "find", "the", "for", "of", "on", "in", "a",
    "an", "my", "please", "open", "watch", "guide", "tutorial", "fru", "cru",
}


class _SolutionPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: List[Tuple[str, str]] = []
        self.media_srcs: List[Tuple[str, str]] = []
        self._cur_href: Optional[str] = None
        self._cur_text: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if t == "a" and attr_map.get("href"):
            self._cur_href = attr_map["href"].strip()
            self._cur_text = []
        elif t in ("video", "source", "iframe") and attr_map.get("src"):
            label = attr_map.get("title") or attr_map.get("aria-label") or ""
            self.media_srcs.append((label.strip(), attr_map["src"].strip()))

    def handle_data(self, data: str) -> None:
        if self._cur_href is not None:
            self._cur_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._cur_href is not None:
            label = " ".join("".join(self._cur_text).split()).strip()
            self.anchors.append((label, self._cur_href))
            self._cur_href = None
            self._cur_text = []


def _normalize_lenovo_solution_url(raw: str) -> Tuple[str, str]:
    ht_m = _HT_ID_RE.search(raw)
    if not ht_m:
        return "", raw
    ht_id = ht_m.group(1).upper()
    locale = "us/en"
    try:
        parsed = urlparse(raw)
        loc_m = re.match(r"^/([a-z]{2}/[a-z]{2})/", parsed.path or "", re.I)
        if loc_m:
            locale = loc_m.group(1).lower()
    except Exception:
        pass
    return ht_id, f"https://pcsupport.lenovo.com/{locale}/solutions/{ht_id.lower()}"


def _is_direct_video_url(url: str) -> bool:
    u = str(url or "").strip()
    if not u.startswith(("http://", "https://")):
        return False
    return bool(_VIDEO_FILE_RE.search(u) or _YOUTUBE_VIDEO_RE.search(u))


def _classify_video_url(url: str) -> str:
    if _YOUTUBE_VIDEO_RE.search(url or ""):
        return "youtube"
    if _VIDEO_FILE_RE.search(url or ""):
        return "mp4"
    return "iframe"


def _title_from_video_url(url: str) -> str:
    yt_m = _YOUTUBE_VIDEO_RE.search(url or "")
    if yt_m:
        return f"YouTube Video ({yt_m.group(1)})"
    try:
        stem = Path(urlparse(url).path).stem
        cleaned = re.sub(r"^[^_]+_(?:FRU|CRU)_\d+_", "", stem, flags=re.I)
        cleaned = re.sub(r"[_\-]+", " ", cleaned).strip()
        return cleaned or stem or "Part Removal Video"
    except Exception:
        return "Part Removal Video"


def _infer_video_action(title: str, url: str) -> str:
    combined = f"{title} {url}".lower()
    if "removal" in combined or "_rem_" in combined or "-rem-" in combined:
        return "removal"
    if "replacement" in combined or "_rep_" in combined or "-rep-" in combined:
        return "replacement"
    return "guide"


def _parse_lenovo_cdn_payload(cdn_text: str) -> Dict[str, Any]:
    m = re.search(r"Object\.assign\(\s*window\.cdnData\s*,\s*(\{.*\})\s*\)\s*;?\s*$", cdn_text or "", re.S)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return {}


def _extract_media_from_html(html_fragment: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    parser = _SolutionPageParser()
    try:
        parser.feed(html_fragment or "")
    except Exception:
        pass

    videos: List[Dict[str, Any]] = []
    manuals: List[Dict[str, str]] = []
    seen_video_urls: Set[str] = set()
    seen_manual_urls: Set[str] = set()

    def _add_video(label: str, href: str) -> None:
        clean_url = (href or "").strip()
        if not clean_url or clean_url in seen_video_urls:
            return
        if clean_url.startswith("//"):
            clean_url = "https:" + clean_url
        elif clean_url.startswith("/"):
            clean_url = "https://pcsupport.lenovo.com" + clean_url
        if not clean_url.startswith(("http://", "https://")):
            return
        vtype = _classify_video_url(clean_url)
        if vtype == "iframe" and not any(h in clean_url.lower() for h in ("youtube", "youtu.be", "vimeo", "brightcove")):
            return
        seen_video_urls.add(clean_url)
        clean_title = (label or "").strip() or _title_from_video_url(clean_url)
        videos.append({
            "title": clean_title,
            "url": clean_url,
            "video_type": vtype,
            "action_type": _infer_video_action(clean_title, clean_url),
            "match_score": 0.0,
        })

    for label, href in parser.anchors:
        low_href = href.lower()
        if _VIDEO_FILE_RE.search(low_href) or _YOUTUBE_VIDEO_RE.search(href):
            _add_video(label, href)
        elif low_href.split("?")[0].endswith(".pdf") and href not in seen_manual_urls:
            full_pdf = f"https://pcsupport.lenovo.com{href}" if href.startswith("/") else href
            seen_manual_urls.add(full_pdf)
            manuals.append({"title": label or "Hardware Maintenance Manual (PDF)", "url": full_pdf})

    for label, src in parser.media_srcs:
        _add_video(label, src)

    # Regex fallback for raw JS/HTML fragments without anchor markup
    if not videos:
        for raw_u in re.findall(r'https?://[^\s"\'<>\\]+\.(?:mp4|webm|m3u8)(?:\?[^\s"\'<>\\]*)?', html_fragment or "", re.I):
            _add_video("", raw_u)
        for yt_m in _YOUTUBE_VIDEO_RE.finditer(html_fragment or ""):
            _add_video("", f"https://www.youtube.com/watch?v={yt_m.group(1)}")

    return videos, manuals


def _score_video_match(video: Dict[str, Any], query_hint: str) -> float:
    hint = (query_hint or "").lower().strip()
    title_low = (video.get("title") or "").lower()
    url_low = (video.get("url") or "").lower()
    hay = f"{title_low} {url_low}"

    score = 0.0
    if video.get("video_type") == "mp4":
        score += 1.0

    want_replace = any(w in hint for w in ("replace", "replacement", "install", "reinstall"))
    want_remove = any(w in hint for w in ("remove", "removal", "disassembl", "take out")) or not want_replace
    action = video.get("action_type", "guide")
    if want_replace and action == "replacement":
        score += 2.5
    elif want_remove and action == "removal":
        score += 2.5
    elif want_remove and action == "replacement":
        score -= 1.0

    raw_tokens = re.findall(r"[a-z0-9.]+", hint)
    meaningful = [t for t in raw_tokens if len(t) >= 2 and t not in _STOP_HINT_TOKENS and not _HT_ID_RE.match(t)]

    # Expand synonyms
    expanded_terms: Set[str] = set()
    for tok in meaningful:
        expanded_terms.add(tok)
        for syn in _PART_SYNONYMS.get(tok, ()):
            expanded_terms.add(syn)

    for term in expanded_terms:
        if re.search(rf"\b{re.escape(term)}\b", title_low):
            score += 8.0
        elif term in title_low:
            score += 5.0
        elif term in url_low:
            score += 3.0

    # Avoid choosing "Disable Built-In Battery" or "Coin Cell Battery" when user simply asks for "battery"
    if meaningful:
        if "disable" not in hint and "disable" in title_low:
            score -= 2.5
        if not any(w in hint for w in ("coin", "cmos", "rtc")) and "coin" in title_low:
            score -= 2.0

    return score


lenovo_kb_service = LenovoKBService()
