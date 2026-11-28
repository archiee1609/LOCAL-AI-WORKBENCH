"""
Lenovo SOPs & Policies Knowledge Service.
Automatically indexes PDFs, image policies (via Windows OCR), and text/Office files
inside the workspace `SOPs & Policies` directory into SQLite + FTS5.
Strictly triggered only when the user asks an SOP, process, or policy question (or /sop).
"""

import base64
import csv
import datetime
import hashlib
import mimetypes
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from pypdf import PdfReader

from app.core.config import settings
from app.core.logging import logger
from app.database.connection import db

_SUPPORTED_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".txt", ".md", ".docx", ".csv", ".xlsx"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from",
    "give", "how", "i", "in", "is", "it", "me", "my", "of", "on", "or", "please", "show",
    "tell", "that", "the", "this", "to", "what", "when", "where", "which", "who", "why",
    "with", "about", "explain", "check", "lenovo", "process",
}

_SOP_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "lfr": ("large", "firm", "retailer"),
    "les": ("lenovo", "exclusive", "store"),
    "cec": ("call", "logging", "case", "phonetic", "escalation"),
    "rps": ("restricted", "party", "screening", "sanctions", "export"),
    "adp": ("accidental", "damage", "protection"),
    "pixel": ("dead", "bright", "dark", "sub-pixel", "lcd", "display"),
    "battery": ("batteries", "stylus", "digitizer", "pen", "12", "months", "warranty"),
    "escalation": ("escalate", "l1", "l1.5", "l2", "flowchart"),
    "phonetic": ("email", "verification", "confirm"),
    "cancellation": ("cancel", "case", "creation"),
}

_EXPLICIT_SOP_CMD_RE = re.compile(r"^(?:/sop|/policy|!sop|!policy)\s*(.*)$", re.I | re.S)

_EXPLICIT_POLICY_PHRASES = (
    "sop", "sops", "standard operating procedure", "policy", "policies",
    "dead pixel", "bright pixel", "sub-pixel", "pixel defect",
    "battery policy", "battery warranty", "adp", "accidental damage protection",
    "warranty policy", "warranty terms", "warranty agreement", "warranty period",
    "restricted party", "rps", "call logging", "lfr", "les",
    "phonetic verification", "case creation", "case cancellation", "cancel a case",
    "email id change", "email modification", "change email id", "modify email",
    "technical escalation", "escalation handling", "l1.5", "cec l2", "cec l1",
)

_SOP_EXCLUSION_CMDS = ("/code", "/pull", "/install", "/image", "/report", "/tips", "/kb", "/psref", "/search", "!code", "!pull", "!tips", "!kb")
_EMAIL_DRAFT_RE = re.compile(r"\b(?:draft|write|compose|prepare|create)\b.{0,28}\b(?:email|mail|reply|response)\b", re.I)


def _clean_text(text: str) -> str:
    if not text:
        return ""
    # Replace Private-Use bullet glyphs often emitted by Word->PDF exports
    cleaned = re.sub(r"[\uf0b7\uf0a7\uf076\u2022\u25cf]+", " • ", str(text))
    cleaned = cleaned.replace("\x00", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in cleaned.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _derive_title_and_category(filename: str) -> Tuple[str, str]:
    stem = Path(filename).stem.strip()
    low = stem.lower()
    if "policy" in low:
        cat = "policy"
    elif "sop" in low or "process" in low or "escalation" in low:
        cat = "sop"
    else:
        cat = "guideline"
    return stem, cat


def _extract_image_ocr_windows(img_path: Path) -> str:
    """Extracts readable text from PNG/JPG SOP images using built-in Windows Media OCR."""
    if os.name != "nt":
        return ""
    resolved = str(img_path.resolve()).replace('"', "")
    ps_script = r'''
$imgPath = "__IMG__"
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType=WindowsRuntime]
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name.StartsWith('IAsyncOperation')
})[0]
function Await($WinRtTask, $ResultType) {
    $netTask = $asTaskGeneric.MakeGenericMethod($ResultType).Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($imgPath)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($engine) {
    $res = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    Write-Output $res.Text
}
'''.replace("__IMG__", resolved)
    try:
        enc = base64.b64encode(ps_script.encode("utf-16le")).decode("ascii")
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-EncodedCommand", enc],
            capture_output=True,
            text=True,
            timeout=20,
            errors="ignore",
        )
        return _clean_text(proc.stdout or "")
    except Exception as exc:
        logger.debug(f"[SOPPolicy] Windows OCR skipped for {img_path.name}: {exc}")
        return ""


class SOPPolicyService:
    """Manages indexing, FTS5 search, strict intent gating, and image/citation formatting for SOPs & Policies."""

    def __init__(self, sop_dir: Optional[Path] = None):
        self._custom_dir = sop_dir
        self._ensured_db_paths: Set[str] = set()
        self._sync_lock = threading.Lock()
        self._last_dir_check: float = 0.0
        self._cached_title_tokens: Set[str] = set()

    @property
    def sop_dir(self) -> Path:
        if self._custom_dir is not None:
            return Path(self._custom_dir)
        return Path(getattr(settings, "sop_policies_dir", settings.base_dir / "SOPs & Policies"))

    def _ensure_tables(self) -> None:
        db_key = str(getattr(db, "db_path", ""))
        if db_key in self._ensured_db_paths:
            return
        try:
            db._executescript_sync(
                """
                CREATE TABLE IF NOT EXISTS sop_files (
                    filename TEXT PRIMARY KEY,
                    sha1 TEXT NOT NULL,
                    mtime REAL NOT NULL,
                    size INTEGER NOT NULL,
                    chunk_count INTEGER DEFAULT 0,
                    synced_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sop_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_key TEXT UNIQUE NOT NULL,
                    source_file TEXT NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT DEFAULT 'sop',
                    page_ref TEXT DEFAULT '',
                    content TEXT NOT NULL,
                    image_path TEXT DEFAULT '',
                    mtime REAL DEFAULT 0,
                    updated_at TEXT DEFAULT ''
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS sop_chunks_fts USING fts5(
                    chunk_id UNINDEXED,
                    source_file,
                    title,
                    category,
                    content,
                    tokenize='porter unicode61'
                );
                """
            )
            self._ensured_db_paths.add(db_key)
        except Exception as exc:
            logger.debug(f"[SOPPolicy] Schema ensure notice: {exc}")

    def _file_sha1(self, path: Path) -> str:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()

    def _extract_file_chunks(self, path: Path, rel_name: str, mtime: float) -> List[Dict[str, Any]]:
        title, category = _derive_title_and_category(path.name)
        ext = path.suffix.lower()
        chunks: List[Dict[str, Any]] = []

        if ext == ".pdf":
            reader = PdfReader(str(path))
            page_entries: List[Tuple[int, str]] = []
            for p_idx, page in enumerate(reader.pages, start=1):
                p_txt = _clean_text(page.extract_text() or "")
                if p_txt:
                    page_entries.append((p_idx, p_txt))
            if not page_entries:
                return []

            # Build coherent ~1200-char chunks preserving page reference
            chunk_idx = 1
            curr_pages: List[int] = []
            curr_buf: List[str] = []
            curr_len = 0

            def _flush():
                nonlocal chunk_idx, curr_pages, curr_buf, curr_len
                if not curr_buf:
                    return
                body = "\n".join(curr_buf).strip()
                if not body:
                    return
                p_ref = (
                    f"Page {curr_pages[0]}"
                    if len(curr_pages) == 1 or curr_pages[0] == curr_pages[-1]
                    else f"Pages {curr_pages[0]}-{curr_pages[-1]}"
                )
                chunks.append({
                    "canonical_key": hashlib.sha1(f"{rel_name}|{chunk_idx}".encode("utf-8")).hexdigest(),
                    "source_file": rel_name,
                    "title": title,
                    "category": category,
                    "page_ref": p_ref,
                    "content": body,
                    "image_path": "",
                    "mtime": mtime,
                })
                chunk_idx += 1
                curr_pages = []
                curr_buf = []
                curr_len = 0

            for p_num, p_txt in page_entries:
                # Split extra-long single pages (e.g. Warranty Policy clauses) into sub-chunks
                pos = 0
                while pos < len(p_txt):
                    slice_txt = p_txt[pos : pos + 1150].strip()
                    pos += 1000
                    if not slice_txt:
                        continue
                    if curr_len + len(slice_txt) > 1250 and curr_buf:
                        _flush()
                    if p_num not in curr_pages:
                        curr_pages.append(p_num)
                    curr_buf.append(slice_txt)
                    curr_len += len(slice_txt)
            _flush()
            return chunks

        if ext in _IMAGE_EXTS:
            ocr_txt = _extract_image_ocr_windows(path)
            content = ocr_txt or f"{title} (Image policy document: {rel_name})"
            return [{
                "canonical_key": hashlib.sha1(f"{rel_name}|img|1".encode("utf-8")).hexdigest(),
                "source_file": rel_name,
                "title": title,
                "category": category,
                "page_ref": "Image",
                "content": content,
                "image_path": str(path.resolve()),
                "mtime": mtime,
            }]

        if ext in {".txt", ".md"}:
            raw = _clean_text(path.read_text(encoding="utf-8", errors="replace"))
        elif ext == ".csv":
            rows_txt = []
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
                for r in csv.reader(f):
                    joined = " | ".join(c.strip() for c in r if c.strip())
                    if joined:
                        rows_txt.append(joined)
            raw = "\n".join(rows_txt)
        elif ext == ".docx":
            from docx import Document
            doc = Document(str(path))
            raw = _clean_text("\n".join(p.text for p in doc.paragraphs if p.text.strip()))
        elif ext == ".xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
            parts = []
            try:
                for ws in wb.worksheets:
                    for row in ws.iter_rows(values_only=True):
                        vals = [str(c).strip() for c in row if c is not None and str(c).strip()]
                        if vals:
                            parts.append(" | ".join(vals))
            finally:
                wb.close()
            raw = "\n".join(parts)
        else:
            return []

        if not raw:
            return []
        pos = 0
        c_idx = 1
        while pos < len(raw):
            piece = raw[pos : pos + 1200].strip()
            pos += 1050
            if piece:
                chunks.append({
                    "canonical_key": hashlib.sha1(f"{rel_name}|{c_idx}".encode("utf-8")).hexdigest(),
                    "source_file": rel_name,
                    "title": title,
                    "category": category,
                    "page_ref": f"Part {c_idx}",
                    "content": piece,
                    "image_path": "",
                    "mtime": mtime,
                })
                c_idx += 1
        return chunks

    def _refresh_title_tokens(self) -> None:
        try:
            rows = db._fetchall_sync("SELECT DISTINCT title FROM sop_chunks")
            toks: Set[str] = set()
            for r in rows:
                for t in re.findall(r"[a-z0-9]+", (r["title"] or "").lower()):
                    if len(t) >= 3 and t not in _STOPWORDS:
                        toks.add(t)
            self._cached_title_tokens = toks
        except Exception:
            pass

    def sync_documents(self, force: bool = False) -> Dict[str, Any]:
        """Incrementally syncs all files in `SOPs & Policies` into SQLite + FTS5."""
        self._ensure_tables()
        base = self.sop_dir
        if not base.exists():
            return {"files": 0, "chunks": 0, "updated": 0}

        with self._sync_lock:
            disk_files: Dict[str, Path] = {}
            for p in sorted(base.rglob("*")):
                if p.is_file() and p.suffix.lower() in _SUPPORTED_EXTS and not p.name.startswith("~$"):
                    rel = p.relative_to(base).as_posix()
                    disk_files[rel] = p

            existing_rows = {r["filename"]: dict(r) for r in db._fetchall_sync("SELECT * FROM sop_files")}
            now = datetime.datetime.now().isoformat()
            updated_count = 0

            # Remove stale entries for deleted files
            removed = set(existing_rows.keys()) - set(disk_files.keys())
            for rel_del in removed:
                db._execute_sync("DELETE FROM sop_chunks_fts WHERE source_file = ?", (rel_del,))
                db._execute_sync("DELETE FROM sop_chunks WHERE source_file = ?", (rel_del,))
                db._execute_sync("DELETE FROM sop_files WHERE filename = ?", (rel_del,))
                updated_count += 1

            for rel, path in disk_files.items():
                st = path.stat()
                prev = existing_rows.get(rel)
                if not force and prev and float(prev.get("mtime") or 0) == st.st_mtime and int(prev.get("size") or 0) == st.st_size:
                    continue
                sha1 = self._file_sha1(path)
                if not force and prev and prev.get("sha1") == sha1:
                    db._execute_sync(
                        "UPDATE sop_files SET mtime = ?, size = ?, synced_at = ? WHERE filename = ?",
                        (st.st_mtime, st.st_size, now, rel),
                    )
                    continue

                try:
                    chunks = self._extract_file_chunks(path, rel, st.st_mtime)
                except Exception as exc:
                    logger.warning(f"[SOPPolicy] Failed parsing '{rel}': {exc}")
                    chunks = []

                db._execute_sync("DELETE FROM sop_chunks_fts WHERE source_file = ?", (rel,))
                db._execute_sync("DELETE FROM sop_chunks WHERE source_file = ?", (rel,))

                for ch in chunks:
                    db._execute_sync(
                        """
                        INSERT INTO sop_chunks (canonical_key, source_file, title, category, page_ref, content, image_path, mtime, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ch["canonical_key"], ch["source_file"], ch["title"], ch["category"],
                            ch["page_ref"], ch["content"], ch["image_path"], ch["mtime"], now,
                        ),
                    )
                inserted = db._fetchall_sync(
                    "SELECT id, source_file, title, category, content FROM sop_chunks WHERE source_file = ?",
                    (rel,),
                )
                for row in inserted:
                    db._execute_sync(
                        "INSERT INTO sop_chunks_fts (chunk_id, source_file, title, category, content) VALUES (?, ?, ?, ?, ?)",
                        (row["id"], row["source_file"], row["title"], row["category"], row["content"]),
                    )

                db._execute_sync(
                    """
                    INSERT INTO sop_files (filename, sha1, mtime, size, chunk_count, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(filename) DO UPDATE SET
                        sha1 = excluded.sha1,
                        mtime = excluded.mtime,
                        size = excluded.size,
                        chunk_count = excluded.chunk_count,
                        synced_at = excluded.synced_at
                    """,
                    (rel, sha1, st.st_mtime, st.st_size, len(chunks), now),
                )
                updated_count += 1

            self._last_dir_check = time.monotonic()
            self._refresh_title_tokens()
            tot_files = db._fetchone_sync("SELECT COUNT(*) AS c FROM sop_files")["c"]
            tot_chunks = db._fetchone_sync("SELECT COUNT(*) AS c FROM sop_chunks")["c"]
            return {"files": int(tot_files), "chunks": int(tot_chunks), "updated": updated_count}

    def _ensure_synced(self) -> None:
        self._ensure_tables()
        if (time.monotonic() - self._last_dir_check) > 3.0:
            self.sync_documents(force=False)

    def detect_chat_sop_intent(self, prompt: str) -> Dict[str, Any]:
        """
        Strictly determines whether the user asked for an SOP, process, or policy from `SOPs & Policies`.
        Never triggers on generic prompts or email drafting unless SOP/policy lookup is explicitly requested.
        """
        cleaned = re.sub(r"\s+", " ", (prompt or "").strip())
        if not cleaned:
            return {"show_sop": False, "explicit": False, "query": ""}

        m_cmd = _EXPLICIT_SOP_CMD_RE.match(cleaned)
        if m_cmd:
            q_part = (m_cmd.group(1) or "").strip() or cleaned
            return {"show_sop": True, "explicit": True, "query": q_part}

        low = cleaned.lower()
        if low.startswith(_SOP_EXCLUSION_CMDS):
            return {"show_sop": False, "explicit": False, "query": ""}

        # Never inject SOPs unprompted when drafting emails unless explicit SOP/policy wording is included
        if _EMAIL_DRAFT_RE.search(cleaned) and not any(k in low for k in ("sop", "policy", "policies", "guideline")):
            return {"show_sop": False, "explicit": False, "query": ""}

        # Explicit SOP / Policy phrase match
        if any(phrase in low for phrase in _EXPLICIT_POLICY_PHRASES):
            return {"show_sop": True, "explicit": False, "query": cleaned}

        # Dynamic check against indexed SOP titles (e.g. any newly added file in SOPs & Policies)
        self._ensure_synced()
        q_tokens = {t for t in re.findall(r"[a-z0-9]+", low) if len(t) >= 3 and t not in _STOPWORDS}
        if len(q_tokens) >= 2:
            rows = db._fetchall_sync("SELECT DISTINCT title FROM sop_chunks")
            for r in rows:
                title_toks = {t for t in re.findall(r"[a-z0-9]+", (r["title"] or "").lower()) if len(t) >= 3 and t not in _STOPWORDS}
                if title_toks and len(q_tokens & title_toks) >= 2:
                    return {"show_sop": True, "explicit": False, "query": cleaned}

        return {"show_sop": False, "explicit": False, "query": ""}

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Searches indexed SOPs & Policies using FTS5 BM25 + document title affinity."""
        self._ensure_synced()
        clean_q = re.sub(r"\s+", " ", (query or "").strip())
        if not clean_q:
            return []

        raw_tokens = [t for t in re.findall(r"[a-z0-9]+", clean_q.lower()) if len(t) >= 2 and t not in _STOPWORDS]
        if not raw_tokens:
            raw_tokens = [t for t in re.findall(r"[a-z0-9]+", clean_q.lower()) if len(t) >= 2]
        if not raw_tokens:
            return []

        expanded: List[str] = []
        seen_t: Set[str] = set()
        for t in raw_tokens:
            if t not in seen_t:
                expanded.append(t)
                seen_t.add(t)
            for syn in _SOP_SYNONYMS.get(t, ()):
                if syn not in seen_t:
                    expanded.append(syn)
                    seen_t.add(syn)

        fts_terms = [f'"{t}"*' for t in expanded[:14]]
        fts_query = " OR ".join(fts_terms)

        candidates: List[Dict[str, Any]] = []
        try:
            rows = db._fetchall_sync(
                """
                SELECT c.*, (-bm25(sop_chunks_fts, 1.0, 2.5, 4.0, 1.5, 1.0)) AS fts_score
                FROM sop_chunks_fts
                JOIN sop_chunks c ON c.id = sop_chunks_fts.chunk_id
                WHERE sop_chunks_fts MATCH ?
                ORDER BY fts_score DESC
                LIMIT ?
                """,
                (fts_query, max(limit * 8, 30)),
            )
            candidates = [dict(r) for r in rows]
        except Exception:
            candidates = []

        if not candidates:
            all_rows = db._fetchall_sync("SELECT *, 1.0 AS fts_score FROM sop_chunks")
            candidates = [dict(r) for r in all_rows]

        scored: List[Dict[str, Any]] = []
        low_q = clean_q.lower()
        for row in candidates:
            title_low = (row.get("title") or "").lower()
            file_low = (row.get("source_file") or "").lower()
            body_low = (row.get("content") or "").lower()
            score = float(row.get("fts_score") or 0.0)

            matched_core = 0
            for t in raw_tokens:
                if t in title_low:
                    score += 18.0
                    matched_core += 1
                elif t in file_low:
                    score += 12.0
                    matched_core += 1
                if re.search(rf"\b{re.escape(t)}\b", body_low):
                    score += 5.0
                    if t not in title_low:
                        matched_core += 1

            # Exact multi-word phrase boosts
            for phrase in (
                "battery policy", "dead pixel", "adp", "accidental damage",
                "restricted party screening", "technical escalation",
                "call logging", "phonetic verification", "case creation",
                "case cancellation", "email id change", "warranty policy",
            ):
                if phrase in low_q and (phrase in title_low or phrase in body_low):
                    score += 35.0

            if matched_core == 0 and score < 5.0:
                continue

            row["score"] = round(score, 2)
            row["image_data_uri"] = ""
            img_p = row.get("image_path") or ""
            if img_p and Path(img_p).exists():
                try:
                    mime = mimetypes.guess_type(img_p)[0] or "image/png"
                    b64 = base64.b64encode(Path(img_p).read_bytes()).decode("ascii")
                    row["image_data_uri"] = f"data:{mime};base64,{b64}"
                except Exception:
                    pass
            scored.append(row)

        scored.sort(key=lambda r: (-r["score"], r["source_file"], r["id"]))

        # Limit max 2 chunks per source document (unless fewer sources matched) for broad, clean coverage
        final_rows: List[Dict[str, Any]] = []
        per_doc: Dict[str, int] = {}
        overflow: List[Dict[str, Any]] = []
        for item in scored:
            src = item["source_file"]
            cnt = per_doc.get(src, 0)
            if cnt < 2:
                final_rows.append(item)
                per_doc[src] = cnt + 1
            else:
                overflow.append(item)
            if len(final_rows) >= limit:
                break
        if len(final_rows) < limit:
            for item in overflow:
                final_rows.append(item)
                if len(final_rows) >= limit:
                    break
        return final_rows[:limit]

    def format_context_for_llm(self, query: str, matches: List[Dict[str, Any]]) -> str:
        if not matches:
            return ""
        lines = [
            f"### 📜 Official Lenovo SOPs & Policies Context (Matched for: '{query}'):",
            "Answer strictly from these SOP & Policy documents. Provide exact rules, steps, and thresholds concisely without inventing policy terms.",
            "",
        ]
        for idx, m in enumerate(matches, start=1):
            lines.append(
                f"[SOP-{idx}] **{m.get('title', 'SOP')}** (`{m.get('source_file', '')}` • {m.get('page_ref', '')})"
            )
            lines.append(m.get("content", "").strip())
            lines.append("")
        return "\n".join(lines).strip()

    def format_sop_citations_markdown(self, matches: List[Dict[str, Any]]) -> str:
        if not matches:
            return ""
        lines = [f"\n\n---\n**📜 SOP & Policy Citations ({len(matches)} match(es)):**"]
        seen_docs: Set[Tuple[str, str]] = set()
        idx = 1
        for m in matches:
            src = m.get("source_file", "")
            pref = m.get("page_ref", "")
            key = (src, pref)
            if key in seen_docs:
                continue
            seen_docs.add(key)
            badge = f"`{src}`" + (f" • {pref}" if pref else "")
            lines.append(f"- **[SOP-{idx}]** {m.get('title', src)} ({badge})")
            idx += 1
        return "\n".join(lines)

    def format_sop_offline_fallback(self, query: str, matches: List[Dict[str, Any]]) -> str:
        if not matches:
            return f"No matching SOP or policy document was found in `SOPs & Policies` for `{query}`."
        lines = [f"### 📜 SOPs & Policies Matches for: `{query}`\n"]
        for idx, m in enumerate(matches, start=1):
            lines.append(f"#### [SOP-{idx}] {m.get('title', '')} (`{m.get('source_file', '')}` • {m.get('page_ref', '')})")
            lines.append(m.get("content", "").strip())
            lines.append("")
        return "\n".join(lines).strip()


sop_policy_service = SOPPolicyService()
