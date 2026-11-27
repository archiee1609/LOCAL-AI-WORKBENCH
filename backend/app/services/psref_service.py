"""
Lenovo PSREF (Product Specifications Reference) Live Lookup Service.
Fetches and parses public hardware specifications from https://psref.lenovo.com/
for Machine Type (MT, e.g. 83J2) and Machine Type Model (MTM, e.g. 83J2003NIN) codes
using an isolated headless Chromium/Edge session with persistent SQLite caching.
"""

import asyncio
import datetime
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import websockets

from app.core.logging import logger
from app.database.connection import db

PSREF_BASE_URL = "https://psref.lenovo.com/"
PSREF_CACHE_TTL_DAYS = 7

_MTM_TOKEN_RE = re.compile(r"\b([A-Z0-9]{10})\b", re.I)
_MT_TOKEN_RE = re.compile(r"\b([A-Z0-9]{4})\b", re.I)
_EXPLICIT_CMD_RE = re.compile(r"^(?:/psref|!psref)\s+(.+)$", re.I)

# Common 4-character alphanumeric tokens in tech chats that are NOT Lenovo Machine Types
_NON_MT_TOKENS = {
    "USB2", "USB3", "USB4", "DDR3", "DDR4", "DDR5", "LPD5", "GEN2", "GEN3", "GEN4", "GEN5",
    "HDMI", "DP14", "DP21", "PCIE", "NVME", "SATA", "WIN1", "WIN7", "WIN8", "WIFI", "WUXG",
    "WQHD", "1080", "1200", "1440", "2160", "16GB", "32GB", "64GB", "8GB1", "12GB", "24GB",
    "48GB", "128G", "256G", "512G", "100W", "135W", "170W", "230W", "300W", "H264", "X264",
    "X265", "SHA1", "SHA2", "MD5S", "HTTP", "HTML", "JSON", "YAML", "XLSX", "DOCX", "PPTX",
    "TPM2", "RJ45", "USBA", "USBC", "FHD1", "UHD1", "OLED", "QHD1", "W11H", "W11P", "MS36",
    "BT53", "BT52", "BT51", "BT50", "8021", "4K60", "2K60",
}

_SPEC_INTENT_KEYWORDS = (
    "psref", "spec", "specs", "specification", "specifications", "config", "configuration",
    "configurations", "datasheet", "hardware spec", "hardware config", "processor", "cpu",
    "gpu", "graphics", "ram", "memory", "max memory", "storage", "ssd", "display", "screen",
    "nits", "srgb", "port", "ports", "battery", "adapter", "charger", "weight", "dimensions",
    "warranty", "chipset", "wifi", "wlan", "bluetooth", "camera", "fingerprint", "touchpad",
    "keyboard", "ports of", "details of", "details for", "info on", "info for", "tell me about",
    "lookup", "check mt", "check mtm", "mtm", "machine type",
)

_TIPS_ONLY_EXCLUSION_KEYWORDS = (
    "known tip", "known tips", "known issue", "known issues", "leap tip", "thursday tech",
    "removal video", "video guide", "part removal", "draft email", "draft a mail", "draft mail",
    "write an email", "compose email",
)

_DETAIL_SECTION_HEADERS = {
    "PERFORMANCE", "DESIGN", "SOFTWARE", "CONNECTIVITY", "SECURITY & PRIVACY",
    "SECURITY", "SERVICE", "ACCESSORIES", "CERTIFICATIONS", "MODEL INFORMATION",
    "OPERATING ENVIRONMENT", "OPERATING REQUIREMENTS",
}

_DETAIL_SKIP_LABELS = {
    "model configurations", "compatible accessories", "recommended services",
    "documentation", "add to compare", "excel", "pdf",
}

_ANTI_BOT_MARKERS = (
    "verify you are human",
    "attention required",
    "cf-browser-verification",
    "captcha",
    "access denied",
)


def _is_valid_mtm_token(token: str) -> bool:
    t = (token or "").strip().upper()
    if len(t) != 10 or not t.isalnum():
        return False
    return any(ch.isdigit() for ch in t) and any(ch.isalpha() for ch in t)


def _is_valid_mt_token(token: str) -> bool:
    t = (token or "").strip().upper()
    if len(t) != 4 or not t.isalnum() or t in _NON_MT_TOKENS:
        return False
    if not (any(ch.isdigit() for ch in t) and any(ch.isalpha() for ch in t)):
        return False
    if re.match(r"^(?:USB|DDR|GEN|PCI|WIN|BT\d|G\d|DP\d|HDMI|SATA)", t):
        return False
    if re.match(r"^\d+(?:GB|MB|TB|HZ|MM|CM|KG|WH|MW|KR|PX|W)$", t):
        return False
    return True


def normalize_psref_query(query: str) -> Dict[str, str]:
    """Classifies user input into normalized query, code, and lookup_type ('mtm', 'mt', or 'keyword')."""
    cleaned = re.sub(r"\s+", " ", (query or "").strip())
    m_cmd = _EXPLICIT_CMD_RE.match(cleaned)
    if m_cmd:
        cleaned = m_cmd.group(1).strip()

    scan_text = re.sub(r"https?://\S+", " ", cleaned)

    # Check explicit MTM token first, then MT token
    for m in _MTM_TOKEN_RE.finditer(scan_text):
        cand = m.group(1).upper()
        if _is_valid_mtm_token(cand):
            return {"query": cand, "code": cand, "lookup_type": "mtm"}

    for m in _MT_TOKEN_RE.finditer(scan_text):
        cand = m.group(1).upper()
        if _is_valid_mt_token(cand):
            return {"query": cand, "code": cand, "lookup_type": "mt"}

    return {"query": cleaned, "code": cleaned.upper(), "lookup_type": "keyword"}


def detect_psref_intent(prompt: str) -> Optional[Dict[str, Any]]:
    """
    Automatically triggers Lenovo PSREF lookup whenever a valid 10-char MTM is entered,
    when a standalone MT/MTM code is entered, when /psref is invoked, or when an MT is paired
    with hardware specification wording.
    """
    cleaned = re.sub(r"\s+", " ", (prompt or "").strip())
    if not cleaned:
        return None
    low = cleaned.lower()
    if low.startswith(("/code ", "/pull ", "/install ", "/image ", "/report ", "!code ", "!pull ")):
        return None

    m_cmd = _EXPLICIT_CMD_RE.match(cleaned)
    if m_cmd:
        norm = normalize_psref_query(m_cmd.group(1))
        if norm["query"]:
            return {**norm, "explicit": True, "wants_specs": True}
        return None

    # Standalone code (e.g. "83J2003NIN", "83J2", "MT 83J2", "MTM: 83J2003NIN")
    standalone = re.sub(r"^(?:mtm|mt)\s*[:#-]?\s*", "", cleaned, flags=re.I).strip()
    if _is_valid_mtm_token(standalone):
        return {"query": standalone.upper(), "code": standalone.upper(), "lookup_type": "mtm", "explicit": True, "wants_specs": True}
    if _is_valid_mt_token(standalone):
        return {"query": standalone.upper(), "code": standalone.upper(), "lookup_type": "mt", "explicit": True, "wants_specs": True}

    norm = normalize_psref_query(cleaned)
    has_spec_kw = any(kw in low for kw in _SPEC_INTENT_KEYWORDS)

    # Whenever the user enters a 10-char MTM anywhere in a message, look up PSREF automatically
    if norm["lookup_type"] == "mtm":
        return {**norm, "explicit": False, "wants_specs": has_spec_kw}

    if norm["lookup_type"] != "mt":
        return None

    if not has_spec_kw:
        return None

    # For 4-char MT inside tips/email prompts without spec keywords above, already returned None;
    # keep explicit spec override clean if both appear.
    has_tips_or_mail = any(kw in low for kw in _TIPS_ONLY_EXCLUSION_KEYWORDS)
    if has_tips_or_mail and not any(k in low for k in ("psref", "spec", "specs", "specification", "specifications", "datasheet", "hardware config")):
        return None

    return {**norm, "explicit": False, "wants_specs": True}


def parse_psref_detail_text(
    raw_text: str,
    psref_url: str = "",
    requested_code: str = "",
    structured_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Parses rendered PSREF /detail/... text (and optional DOM row pairs) into structured sections."""
    lines = [line.rstrip() for line in (raw_text or "").splitlines()]
    product_name = ""
    model_code = (requested_code or "").upper()
    withdrawn = "(Withdrawn)" in (raw_text or "")

    # Extract product heading & model code cleanly
    for idx, line in enumerate(lines[:40]):
        s = line.strip()
        if not s:
            continue
        if s.startswith("Model:"):
            cand = s.split(":", 1)[1].strip().upper()
            if cand:
                model_code = cand
            # Preceding non-nav, non-Withdrawn line is usually product name
            for back in range(idx - 1, -1, -1):
                prev = lines[back].strip()
                if prev and prev not in ("(Withdrawn)", "Add to compare") and not prev.startswith(("Laptops", "Tablets", "Desktops", "Workstations", "Monitors", "Smart Collaboration", "Edge Devices")):
                    product_name = prev.replace("(Withdrawn)", "").strip()
                    break
            break

    sections: List[Dict[str, Any]] = []
    if structured_rows:
        # Expect list of {"section": str, "items": [{"label": str, "value": str}]}
        for sec in structured_rows:
            s_title = str(sec.get("section") or "").strip()
            s_items = [
                {"label": str(it.get("label") or "").strip(), "value": str(it.get("value") or "").strip()}
                for it in (sec.get("items") or [])
                if str(it.get("label") or "").strip() and str(it.get("value") or "").strip()
            ]
            if s_title and s_items:
                sections.append({"section": s_title, "items": s_items})

    if not sections:
        curr_section = ""
        curr_items: List[Tuple[str, str]] = []
        pending_label = ""
        value_lines: List[str] = []

        def _flush_pair():
            nonlocal pending_label, value_lines
            if pending_label and value_lines:
                val = "\n".join(v for v in value_lines if v).strip()
                if val:
                    curr_items.append((pending_label, val))
            pending_label = ""
            value_lines = []

        def _flush_section():
            nonlocal curr_section, curr_items
            _flush_pair()
            if curr_section and curr_items:
                sections.append({
                    "section": curr_section,
                    "items": [{"label": k, "value": v} for k, v in curr_items],
                })
            curr_items = []

        started = False
        for raw_line in lines:
            stripped = raw_line.strip()
            low = stripped.lower()
            if stripped.startswith(("Note :", "Note:", "Accessories & Services", "The PSREF website is a specification query platform", "Cookie Consent")):
                break
            if stripped in _DETAIL_SECTION_HEADERS:
                started = True
                _flush_section()
                curr_section = stripped
                continue
            if not started:
                continue
            if not stripped:
                continue
            if low in _DETAIL_SKIP_LABELS:
                continue
            # PSREF detail view appends '\t' on label lines
            if raw_line.endswith("\t"):
                _flush_pair()
                pending_label = stripped
            elif not pending_label:
                pending_label = stripped
            else:
                value_lines.append(stripped)

        _flush_section()

    # Pull machine type & key highlights if available
    machine_type = ""
    for sec in sections:
        for it in sec["items"]:
            if it["label"].lower() == "machine type":
                machine_type = it["value"].strip()
                break

    return {
        "found": bool(sections or product_name),
        "lookup_type": "mtm",
        "requested_code": (requested_code or model_code).upper(),
        "model_code": model_code,
        "machine_type": machine_type or (model_code[:4] if len(model_code) >= 4 else ""),
        "product_name": product_name or requested_code,
        "withdrawn": withdrawn,
        "psref_url": psref_url,
        "sections": sections,
        "sample_models": [],
        "total_models_hint": "",
    }


def parse_psref_search_summary(raw_text: str, links: List[Dict[str, str]]) -> Dict[str, Any]:
    """Extracts matched product line, spec URL, sample model configurations, and total count from PSREF /search/."""
    product_name = ""
    spec_url = ""
    product_url = ""
    withdrawn = "(Withdrawn)" in (raw_text or "")

    for lnk in links or []:
        href = str(lnk.get("href") or "")
        text = str(lnk.get("text") or "").strip()
        if not spec_url and "tab=spec" in href:
            spec_url = href if href.startswith("http") else f"https://psref.lenovo.com{href}"
        if not product_url and "/Product/" in href and "?" not in href:
            product_url = href if href.startswith("http") else f"https://psref.lenovo.com{href}"
            if text:
                product_name = text.replace("(Withdrawn)", "").strip()

    if not spec_url and product_url:
        spec_url = f"{product_url}?tab=spec"

    # Extract sample model summaries rendered directly on the search card
    sample_models: List[Dict[str, str]] = []
    model_blocks = re.split(r"(?=^Model:\s*[A-Z0-9]{6,14})", raw_text or "", flags=re.M)
    for blk in model_blocks:
        m_mod = re.match(r"^Model:\s*([A-Z0-9]{6,14})", blk.strip())
        if not m_mod:
            continue
        code = m_mod.group(1)
        fields: Dict[str, str] = {"model": code}
        for line in blk.splitlines()[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                ks = k.strip()
                vs = v.strip()
                if ks in ("Processor", "Memory", "Storage", "Graphics", "Operating System", "Base Warranty"):
                    fields[ks] = vs
        sample_models.append(fields)

    m_total = re.search(r"See all matching models\s*\((\d+)\)", raw_text or "", re.I)
    total_hint = m_total.group(1) if m_total else ""

    m_res = re.search(r"(\d+)\s+results?\s+in\s+(?:withdrawn\s+)?products", raw_text or "", re.I)
    results_count = int(m_res.group(1)) if m_res else (1 if (product_name or sample_models) else 0)

    return {
        "results_count": results_count,
        "product_name": product_name,
        "withdrawn": withdrawn,
        "product_url": product_url,
        "spec_url": spec_url,
        "sample_models": sample_models[:6],
        "total_models_hint": total_hint,
    }


def parse_psref_platform_spec_text(
    raw_text: str,
    psref_url: str = "",
    requested_code: str = "",
    search_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Parses rendered PSREF /Product/...?tab=spec text into structured Platform Specification sections."""
    lines = [line.strip() for line in (raw_text or "").splitlines()]
    search_meta = search_meta or {}
    product_name = search_meta.get("product_name", "")
    withdrawn = bool(search_meta.get("withdrawn")) or "(Withdrawn)" in (raw_text or "")

    if not product_name:
        for idx, s in enumerate(lines[:35]):
            if s == "Overview" and idx > 0:
                prev = lines[idx - 1].replace("(Withdrawn)", "").strip()
                if prev:
                    product_name = prev
                break

    # Slice starting from PERFORMANCE header after TOC
    start_idx = 0
    for idx, s in enumerate(lines):
        if s == "PERFORMANCE" and idx > 15:
            start_idx = idx
            break
    if start_idx == 0:
        for idx, s in enumerate(lines):
            if s == "Platform Specifications":
                start_idx = idx + 1
                break

    major_headers = {
        "PERFORMANCE", "DESIGN", "CONNECTIVITY", "SECURITY & PRIVACY",
        "SERVICE", "ACCESSORIES", "OPERATING REQUIREMENTS", "CERTIFICATIONS",
    }
    sections: List[Dict[str, Any]] = []
    curr_major = "PLATFORM SPECIFICATIONS"
    curr_items: List[Dict[str, str]] = []
    curr_sub = ""
    buf: List[str] = []

    def _flush_sub():
        nonlocal curr_sub, buf
        if curr_sub and buf:
            cleaned_vals = []
            for ln in buf:
                if "\t" in ln:
                    cleaned_vals.append(" | ".join(c.strip() for c in ln.split("\t") if c.strip()))
                else:
                    cleaned_vals.append(ln)
            val_text = "\n".join(v for v in cleaned_vals if v).strip()
            if val_text:
                curr_items.append({"label": curr_sub.rstrip("*"), "value": val_text})
        curr_sub = ""
        buf = []

    def _flush_major():
        nonlocal curr_items
        _flush_sub()
        if curr_items:
            sections.append({"section": curr_major, "items": curr_items})
        curr_items = []

    known_subheadings = {
        "Processor Family", "Processor**", "Processor", "Operating System**", "Operating System",
        "Graphics**", "Graphics", "Monitor Support", "Chipset", "Max Memory", "Memory Slots**",
        "Memory Slots", "Memory Type", "Memory", "Max Storage Support", "Storage Slot", "Storage Type",
        "Storage", "Removable Storage", "Optical", "Card Reader", "Audio Chip", "Speakers",
        "Microphone", "Camera", "Battery", "Max Battery Life", "Power Adapter", "Display",
        "Touchscreen", "Screen-to-Body Ratio", "Pen", "Keyboard", "Touchpad", "Dimensions (WxDxH)",
        "Weight", "Case Color", "Case Material", "Surface Treatment", "WLAN + Bluetooth",
        "Ethernet", "Standard Ports", "Monitor Cable", "Docking", "Security Chip",
        "Fingerprint Reader", "Other Security", "Base Warranty", "Bundled Accessories",
        "Operating Environment", "Green Certifications", "Mil-Spec Test", "Other Certifications",
    }

    for s in lines[start_idx:]:
        if not s:
            continue
        if s.startswith(("The PSREF website is a specification query platform", "Cookie Consent", "© 2026 Lenovo", "© 2025 Lenovo")):
            break
        if s in major_headers:
            _flush_major()
            curr_major = s
            continue
        # Strip trailing footnote markers like Storage[2] -> Storage
        base_s = re.sub(r"\[\d+\]$", "", s).strip()
        if base_s in known_subheadings or s.endswith("**"):
            _flush_sub()
            curr_sub = base_s
        else:
            if not curr_sub:
                curr_sub = base_s
            else:
                buf.append(s)

    _flush_major()

    return {
        "found": bool(sections or search_meta.get("sample_models") or product_name),
        "lookup_type": "mt",
        "requested_code": (requested_code or "").upper(),
        "model_code": "",
        "machine_type": (requested_code or "").upper()[:4],
        "product_name": product_name or requested_code,
        "withdrawn": withdrawn,
        "psref_url": psref_url or search_meta.get("product_url") or PSREF_BASE_URL,
        "sections": sections,
        "sample_models": search_meta.get("sample_models") or [],
        "total_models_hint": search_meta.get("total_models_hint") or "",
    }


def format_psref_markdown(spec: Dict[str, Any]) -> str:
    """Renders structured PSREF spec payload as crisp Markdown for UI/fallback display."""
    if not spec or not spec.get("found"):
        code = (spec or {}).get("requested_code", "")
        err = (spec or {}).get("error", "")
        if err:
            return f"⚠️ Could not retrieve PSREF specifications for `{code}`: {err}"
        return f"No matching Lenovo PSREF product was found for `{code}`."

    product_name = spec.get("product_name") or spec.get("requested_code")
    code = spec.get("model_code") or spec.get("requested_code")
    ltype = "MTM" if spec.get("lookup_type") == "mtm" else "Machine Type (MT)"
    status_badge = " *(Withdrawn)*" if spec.get("withdrawn") else ""
    url = spec.get("psref_url") or PSREF_BASE_URL

    lines = [
        f"### 📋 Lenovo PSREF Specifications — {product_name}{status_badge}",
        f"**{ltype}:** `{code}` • **Official Source:** [Open in Lenovo PSREF]({url})\n",
    ]

    for sec in spec.get("sections") or []:
        items = sec.get("items") or []
        if not items:
            continue
        lines.append(f"#### {sec.get('section', 'SPECIFICATIONS')}")
        lines.append("| Specification | Details |")
        lines.append("| :--- | :--- |")
        for it in items:
            lbl = str(it.get("label") or "").replace("|", "\\|").strip()
            val = re.sub(r"\s*\n+\s*", "<br>", str(it.get("value") or "").replace("|", "\\|").strip())
            if lbl and val:
                lines.append(f"| **{lbl}** | {val} |")
        lines.append("")

    sample_models = spec.get("sample_models") or []
    if sample_models:
        total_hint = f" ({spec['total_models_hint']} total on PSREF)" if spec.get("total_models_hint") else ""
        lines.append(f"#### Matching Configurations{total_hint}")
        for mod in sample_models[:4]:
            parts = [f"**`{mod.get('model', '')}`**"]
            for k in ("Processor", "Memory", "Storage", "Graphics", "Operating System"):
                if mod.get(k):
                    parts.append(f"{k}: {mod[k]}")
            lines.append("- " + " • ".join(parts))
        lines.append("")

    if spec.get("cached"):
        lines.append(f"> ℹ️ *Loaded from local PSREF cache (`fetched {spec.get('fetched_at', '')[:16]}`).*")

    return "\n".join(lines).strip()


def format_psref_llm_context(spec: Dict[str, Any]) -> str:
    """Formats structured PSREF specifications into high-density LLM system context."""
    if not spec or not spec.get("found"):
        return ""
    lines = [
        "### Official Lenovo PSREF Hardware Specifications (Retrieved Live from psref.lenovo.com):",
        f"- **Product Name:** {spec.get('product_name', '')}",
        f"- **Queried Code:** {spec.get('requested_code', '')} ({spec.get('lookup_type', '').upper()})",
        f"- **Official PSREF Link:** {spec.get('psref_url', PSREF_BASE_URL)}",
    ]
    if spec.get("withdrawn"):
        lines.append("- **Lifecycle Status:** Withdrawn")
    for sec in spec.get("sections") or []:
        lines.append(f"\n[{sec.get('section', '')}]")
        for it in sec.get("items") or []:
            val = re.sub(r"\s*\n+\s*", "; ", str(it.get("value") or "").strip())
            lines.append(f"- {it.get('label', '')}: {val}")
    for mod in (spec.get("sample_models") or [])[:4]:
        lines.append(
            f"- Sample MTM {mod.get('model', '')}: CPU={mod.get('Processor', 'N/A')}, "
            f"RAM={mod.get('Memory', 'N/A')}, Storage={mod.get('Storage', 'N/A')}, GPU={mod.get('Graphics', 'N/A')}"
        )
    lines.append(
        "\nInstructions: Answer the user's question directly and accurately using ONLY these verified PSREF specifications. "
        "Format clearly (bullet points or compact table) and include the official PSREF link."
    )
    return "\n".join(lines)


class LenovoPSREFService:
    """Manages live PSREF specification lookups via headless Edge/Chrome CDP + SQLite cache."""

    def __init__(self):
        self._ensured_db_paths: set[str] = set()

    def _ensure_cache_table(self) -> None:
        db_key = str(getattr(db, "db_path", ""))
        if db_key in self._ensured_db_paths:
            return
        try:
            db._execute_sync(
                """
                CREATE TABLE IF NOT EXISTS psref_cache (
                    code TEXT PRIMARY KEY,
                    lookup_type TEXT DEFAULT 'mtm',
                    product_name TEXT DEFAULT '',
                    psref_url TEXT DEFAULT '',
                    payload_json TEXT DEFAULT '{}',
                    fetched_at TEXT NOT NULL
                )
                """
            )
            self._ensured_db_paths.add(db_key)
        except Exception as exc:
            logger.debug(f"[LenovoPSREF] Cache table check notice: {exc}")

    def get_cached(self, code: str) -> Optional[Dict[str, Any]]:
        norm_code = (code or "").strip().upper()
        if not norm_code:
            return None
        self._ensure_cache_table()
        try:
            row = db._fetchone_sync("SELECT * FROM psref_cache WHERE code = ?", (norm_code,))
            if not row:
                return None
            fetched_at = row["fetched_at"] or ""
            try:
                dt = datetime.datetime.fromisoformat(fetched_at)
                if (datetime.datetime.now() - dt).days >= PSREF_CACHE_TTL_DAYS:
                    return None
            except Exception:
                pass
            payload = json.loads(row["payload_json"] or "{}")
            if isinstance(payload, dict) and payload.get("found"):
                payload["cached"] = True
                payload["fetched_at"] = fetched_at
                return payload
        except Exception as exc:
            logger.debug(f"[LenovoPSREF] Cache read notice for {norm_code}: {exc}")
        return None

    def save_cached(self, code: str, payload: Dict[str, Any]) -> None:
        norm_code = (code or "").strip().upper()
        if not norm_code or not payload or not payload.get("found"):
            return
        self._ensure_cache_table()
        now = datetime.datetime.now().isoformat()
        clean_payload = dict(payload)
        clean_payload["cached"] = False
        clean_payload["fetched_at"] = now
        try:
            db._execute_sync(
                """
                INSERT INTO psref_cache (code, lookup_type, product_name, psref_url, payload_json, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    lookup_type = excluded.lookup_type,
                    product_name = excluded.product_name,
                    psref_url = excluded.psref_url,
                    payload_json = excluded.payload_json,
                    fetched_at = excluded.fetched_at
                """,
                (
                    norm_code,
                    str(payload.get("lookup_type") or "mtm"),
                    str(payload.get("product_name") or norm_code),
                    str(payload.get("psref_url") or PSREF_BASE_URL),
                    json.dumps(clean_payload, ensure_ascii=False),
                    now,
                ),
            )
        except Exception as exc:
            logger.debug(f"[LenovoPSREF] Cache write notice for {norm_code}: {exc}")

    def list_recent_cached(self, limit: int = 12) -> List[Dict[str, Any]]:
        self._ensure_cache_table()
        try:
            rows = db._fetchall_sync(
                "SELECT code, lookup_type, product_name, psref_url, fetched_at FROM psref_cache ORDER BY fetched_at DESC LIMIT ?",
                (limit,),
            )
            return [dict(r) for r in rows]
        except Exception:
            return []

    @staticmethod
    def find_browser_executable() -> Optional[str]:
        """Finds an installed Chromium/Edge binary for headless PSREF SPA rendering."""
        candidates = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        for exe in candidates:
            if Path(exe).exists():
                return exe
        for cmd in ("msedge", "microsoft-edge", "google-chrome", "chromium", "chrome"):
            found = shutil.which(cmd)
            if found:
                return found
        return None

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    async def _fetch_live_via_cdp(self, query: str, lookup_type: str) -> Dict[str, Any]:
        browser_exe = self.find_browser_executable()
        if not browser_exe:
            return {
                "found": False,
                "lookup_type": lookup_type,
                "requested_code": query,
                "psref_url": PSREF_BASE_URL,
                "sections": [],
                "error": "No Chromium or Microsoft Edge browser was found on this system to render PSREF.",
            }

        port = self._free_port()
        tmpdir = tempfile.mkdtemp(prefix="psref_cdp_")
        proc = subprocess.Popen(
            [
                browser_exe,
                "--headless=new",
                "--disable-gpu",
                "--disable-extensions",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={tmpdir}",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            ws_url = None
            for _ in range(40):
                if proc.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as resp:
                        pages = [p for p in json.loads(resp.read().decode()) if p.get("type") == "page"]
                        if pages:
                            ws_url = pages[0]["webSocketDebuggerUrl"]
                            break
                except Exception:
                    await asyncio.sleep(0.2)

            if not ws_url:
                return {
                    "found": False,
                    "lookup_type": lookup_type,
                    "requested_code": query,
                    "psref_url": PSREF_BASE_URL,
                    "sections": [],
                    "error": "Headless browser debug port did not initialize in time.",
                }

            async with websockets.connect(ws_url, max_size=16_000_000) as ws:
                msg_id = 0

                async def cdp(method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
                    nonlocal msg_id
                    msg_id += 1
                    mid = msg_id
                    await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=15)
                        data = json.loads(raw)
                        if data.get("id") == mid:
                            return data

                async def eval_val(expr: str) -> Any:
                    try:
                        res = await cdp("Runtime.evaluate", {"expression": expr, "returnByValue": True})
                        return res.get("result", {}).get("result", {}).get("value")
                    except Exception:
                        return None

                await cdp("Page.enable")
                await cdp("Runtime.enable")
                await cdp("Page.navigate", {"url": PSREF_BASE_URL})

                input_ready = False
                for _ in range(40):
                    val = await eval_val(
                        'document.querySelector("input[type=\'text\'], input:not([type])") ? "READY" : ""'
                    )
                    if val == "READY":
                        input_ready = True
                        break
                    await asyncio.sleep(0.25)

                if not input_ready:
                    body_low = (await eval_val("document.body ? document.body.innerText : ''") or "").lower()
                    if any(marker in body_low for marker in _ANTI_BOT_MARKERS):
                        return {
                            "found": False,
                            "lookup_type": lookup_type,
                            "requested_code": query,
                            "psref_url": PSREF_BASE_URL,
                            "sections": [],
                            "error": "psref.lenovo.com returned a verification/access check page.",
                        }
                    return {
                        "found": False,
                        "lookup_type": lookup_type,
                        "requested_code": query,
                        "psref_url": PSREF_BASE_URL,
                        "sections": [],
                        "error": "PSREF search box did not render cleanly (site may be unreachable).",
                    }

                await eval_val('document.querySelector("input[type=\'text\'], input:not([type])").focus()')
                await asyncio.sleep(0.15)
                await cdp("Input.insertText", {"text": query})
                await asyncio.sleep(0.3)
                await cdp(
                    "Input.dispatchKeyEvent",
                    {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13},
                )
                await cdp(
                    "Input.dispatchKeyEvent",
                    {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13},
                )

                final_url = PSREF_BASE_URL
                final_text = ""
                links_json = "[]"
                landed = False
                for _ in range(30):
                    await asyncio.sleep(0.5)
                    url = await eval_val("location.href") or ""
                    txt = await eval_val("document.body ? document.body.innerText : ''") or ""
                    low_txt = txt.lower()
                    if any(marker in low_txt for marker in _ANTI_BOT_MARKERS):
                        return {
                            "found": False,
                            "lookup_type": lookup_type,
                            "requested_code": query,
                            "psref_url": url or PSREF_BASE_URL,
                            "sections": [],
                            "error": "psref.lenovo.com presented an anti-bot or access verification challenge.",
                        }
                    if "/detail/" in url.lower() and ("PERFORMANCE" in txt or "Processor" in txt):
                        await asyncio.sleep(0.5)
                        final_url = await eval_val("location.href") or url
                        final_text = await eval_val("document.body ? document.body.innerText : ''") or txt
                        landed = True
                        break
                    if "/search" in url.lower() and (
                        "results in" in low_txt
                        or "no matching" in low_txt
                        or "0 results" in low_txt
                        or "Model:" in txt
                    ):
                        await asyncio.sleep(0.5)
                        final_url = await eval_val("location.href") or url
                        final_text = await eval_val("document.body ? document.body.innerText : ''") or txt
                        links_json = await eval_val(
                            'JSON.stringify([...document.querySelectorAll("a[href]")].map(a=>({text:(a.innerText||"").trim(),href:a.getAttribute("href")})).filter(x=>x.href && (x.href.includes("/Product/") || x.href.includes("/Detail/"))).slice(0, 20))'
                        ) or "[]"
                        landed = True
                        break

                if not landed:
                    final_url = await eval_val("location.href") or PSREF_BASE_URL
                    final_text = await eval_val("document.body ? document.body.innerText : ''") or ""

                # Exact MTM detail page
                if "/detail/" in final_url.lower():
                    rows_json = await eval_val(
                        '''JSON.stringify((() => {
                            const out = [];
                            let curr = null;
                            document.querySelectorAll('table tr').forEach(tr => {
                                const cells = [...tr.children].map(c => (c.innerText || '').trim()).filter(Boolean);
                                if (cells.length === 1 && /^[A-Z0-9 &/-]{3,32}$/.test(cells[0]) && cells[0] === cells[0].toUpperCase()) {
                                    curr = { section: cells[0], items: [] };
                                    out.push(curr);
                                } else if (cells.length >= 2 && curr) {
                                    curr.items.push({ label: cells[0], value: cells.slice(1).join(' | ') });
                                }
                            });
                            return out;
                        })())'''
                    )
                    structured_rows = json.loads(rows_json) if isinstance(rows_json, str) else None
                    return parse_psref_detail_text(
                        final_text,
                        psref_url=final_url,
                        requested_code=query,
                        structured_rows=structured_rows,
                    )

                # Search results page (typical for 4-char MT like 83J2 or keyword queries)
                links = json.loads(links_json) if isinstance(links_json, str) else []
                search_meta = parse_psref_search_summary(final_text, links)
                if search_meta.get("results_count", 0) == 0 and not search_meta.get("spec_url"):
                    return {
                        "found": False,
                        "lookup_type": lookup_type,
                        "requested_code": query,
                        "psref_url": final_url,
                        "sections": [],
                        "error": f"No matching product specifications found on PSREF for '{query}'.",
                    }

                spec_url = search_meta.get("spec_url")
                if spec_url:
                    await cdp("Page.navigate", {"url": spec_url})
                    spec_text = ""
                    for _ in range(30):
                        await asyncio.sleep(0.5)
                        txt = await eval_val("document.body ? document.body.innerText : ''") or ""
                        if "Processor" in txt and ("Platform Specifications" in txt or "PERFORMANCE" in txt):
                            await asyncio.sleep(0.5)
                            spec_text = await eval_val("document.body ? document.body.innerText : ''") or txt
                            break
                    if spec_text:
                        return parse_psref_platform_spec_text(
                            spec_text,
                            psref_url=spec_url,
                            requested_code=query,
                            search_meta=search_meta,
                        )

                # Fallback using search summary cards
                parsed_fb = parse_psref_platform_spec_text(
                    "",
                    psref_url=search_meta.get("product_url") or final_url,
                    requested_code=query,
                    search_meta=search_meta,
                )
                return parsed_fb
        except Exception as exc:
            logger.warning(f"[LenovoPSREF] Lookup failed for '{query}': {exc}")
            return {
                "found": False,
                "lookup_type": lookup_type,
                "requested_code": query,
                "psref_url": PSREF_BASE_URL,
                "sections": [],
                "error": f"PSREF lookup error: {exc}",
            }
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def lookup(self, query: str, force_refresh: bool = False) -> Dict[str, Any]:
        """Looks up Lenovo PSREF specifications by MT, MTM, or product keyword (cached in SQLite)."""
        norm = normalize_psref_query(query)
        q_code = norm["code"]
        q_val = norm["query"]
        ltype = norm["lookup_type"]

        if not q_val:
            return {
                "found": False,
                "lookup_type": ltype,
                "requested_code": "",
                "psref_url": PSREF_BASE_URL,
                "sections": [],
                "error": "Please enter a valid MT (e.g. 83J2) or MTM (e.g. 83J2003NIN).",
            }

        if not force_refresh:
            cached = self.get_cached(q_code)
            if cached:
                return cached

        result = await self._fetch_live_via_cdp(q_val, ltype)
        if result.get("found"):
            self.save_cached(q_code, result)
            # Also save under normalized model_code if different (e.g. MTM returned for keyword query)
            mod_code = (result.get("model_code") or "").upper()
            if mod_code and mod_code != q_code:
                self.save_cached(mod_code, result)
        return result

    def lookup_sync(self, query: str, force_refresh: bool = False) -> Dict[str, Any]:
        """Thread-safe synchronous wrapper for Streamlit UI callers."""
        try:
            asyncio.get_running_loop()
            holder: Dict[str, Any] = {}

            def _runner():
                holder["res"] = asyncio.run(self.lookup(query, force_refresh=force_refresh))

            t = threading.Thread(target=_runner, daemon=True)
            t.start()
            t.join()
            return holder["res"]
        except RuntimeError:
            return asyncio.run(self.lookup(query, force_refresh=force_refresh))


psref_service = LenovoPSREFService()
