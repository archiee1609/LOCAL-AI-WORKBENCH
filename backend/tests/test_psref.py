import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from app.database.connection import db
from app.main import app
from app.services.psref_service import (
    LenovoPSREFService,
    detect_psref_intent,
    format_psref_llm_context,
    format_psref_markdown,
    normalize_psref_query,
    parse_psref_detail_text,
    parse_psref_platform_spec_text,
    parse_psref_search_summary,
    psref_service,
)
from app.services.tool_registry import tool_registry

client = TestClient(app)

SAMPLE_DETAIL_TEXT = """Laptops
Tablets
IdeaPad Slim 5 13ARP10
(Withdrawn)
Model: 83J2003NIN
Add to compare
Compare (
0
/10)
Excel
PDF
PERFORMANCE
Processor\t
AMD Ryzen™ 7 7735HS (8C / 16T, 3.2 / 4.75GHz, 4MB L2 / 16MB L3)
Graphics\t
Integrated AMD Radeon™ 680M Graphics
Memory\t
16GB Soldered LPDDR5X-6400
Storage\t
512GB SSD M.2 2242 PCIe® 4.0x4 NVMe®
DESIGN
Display\t
13.3" WUXGA (1920x1200) IPS 400nits Anti-glare, 100% sRGB
Weight\t
Around 1.15 kg (2.54 lbs)
CONNECTIVITY
Standard Ports\t
1x USB-A (USB 5Gbps / USB 3.2 Gen 1)
2x USB-C® (USB 10Gbps / USB 3.2 Gen 2), with USB PD 45-65W and DisplayPort™ 1.4
1x HDMI® 2.1, up to 4K/60Hz
MODEL INFORMATION
Model\t
83J2003NIN
Machine Type\t
83J2
Region\t
INDIA
Announce Date\t
2025-02-17
Note :
Lenovo reserves the right to change specifications.
"""

SAMPLE_SEARCH_TEXT = """PSREF
1 results in withdrawn products
IdeaPad Slim 5 13ARP10 (Withdrawn)
Models
Specifications
Model: 83J2001DSC
Processor: AMD Ryzen 5 7535HS (6C / 12T)
Memory: 16GB Soldered LPDDR5X-6400
Storage: 512GB SSD M.2 2242 PCIe 4.0x4 NVMe
Graphics: Integrated AMD Radeon 660M Graphics
See all matching models (236)
"""

SAMPLE_PLATFORM_SPEC_TEXT = """IdeaPad Slim 5 13ARP10 (Withdrawn)
Platform Specifications
Performance
Processor
Design
PERFORMANCE
Processor
Processor Family
AMD Ryzen™ 5 / 7 7000 Series Processor
Processor**
Processor Name\tCores\tThreads\tBase Frequency
AMD Ryzen™ 7 7735HS\t8\t16\t3.2GHz
Memory
Max Memory
16GB soldered memory, not upgradable
Storage
Max Storage Support[1]
One drive, up to 1TB M.2 2242 SSD
DESIGN
Display
13.3" WUXGA (1920x1200) IPS 400nits Anti-glare
The PSREF website is a specification query platform.
"""


def test_psref_query_normalization_and_intent_gating():
    assert normalize_psref_query("83J2003NIN") == {
        "query": "83J2003NIN",
        "code": "83J2003NIN",
        "lookup_type": "mtm",
    }
    assert normalize_psref_query("83J2") == {
        "query": "83J2",
        "code": "83J2",
        "lookup_type": "mt",
    }
    assert normalize_psref_query("/psref 21KC00C1SG")["code"] == "21KC00C1SG"

    # Standalone & /psref commands
    assert detect_psref_intent("83J2003NIN") == {
        "query": "83J2003NIN",
        "code": "83J2003NIN",
        "lookup_type": "mtm",
        "explicit": True,
        "wants_specs": True,
    }
    assert detect_psref_intent("MT 83J2") == {
        "query": "83J2",
        "code": "83J2",
        "lookup_type": "mt",
        "explicit": True,
        "wants_specs": True,
    }
    assert detect_psref_intent("/psref 83J2")["explicit"] is True

    # Conversational prompts with an MTM ALWAYS trigger automatic PSREF lookup
    conv_intent = detect_psref_intent("What processor and RAM does 83J2003NIN have?")
    assert conv_intent is not None
    assert conv_intent["code"] == "83J2003NIN"
    assert conv_intent["explicit"] is False
    assert conv_intent["wants_specs"] is True

    any_mtm_mail = detect_psref_intent("draft an email to customer about 83J2003NIN")
    assert any_mtm_mail is not None
    assert any_mtm_mail["code"] == "83J2003NIN"
    assert any_mtm_mail["lookup_type"] == "mtm"
    assert any_mtm_mail["wants_specs"] is False

    any_mtm_check = detect_psref_intent("check 83J2003NIN")
    assert any_mtm_check is not None and any_mtm_check["code"] == "83J2003NIN"

    # Non-spec requests with only 4-char MT or non-MT tech tokens must NOT trigger PSREF
    assert detect_psref_intent("draft an email to customer about 83J2 issue") is None
    assert detect_psref_intent("show known tips for 83J2") is None
    assert detect_psref_intent("show hardware removal video for 83DV") is None
    assert detect_psref_intent("Does it support USB3 and DDR5 HDMI?") is None
    assert detect_psref_intent("See https://example.com/path/83J2003NIN for details") is None


def test_parse_psref_detail_and_platform_specs_and_formatters():
    detail = parse_psref_detail_text(
        SAMPLE_DETAIL_TEXT,
        psref_url="https://psref.lenovo.com/detail/IdeaPad_Slim_5_13ARP10?M=83J2003NIN",
        requested_code="83J2003NIN",
    )
    assert detail["found"] is True
    assert detail["product_name"] == "IdeaPad Slim 5 13ARP10"
    assert detail["model_code"] == "83J2003NIN"
    assert detail["machine_type"] == "83J2"
    assert detail["withdrawn"] is True
    sec_names = [s["section"] for s in detail["sections"]]
    assert "PERFORMANCE" in sec_names
    assert "CONNECTIVITY" in sec_names

    md = format_psref_markdown(detail)
    assert "IdeaPad Slim 5 13ARP10" in md
    assert "83J2003NIN" in md
    assert "AMD Ryzen™ 7 7735HS" in md
    assert "https://psref.lenovo.com/detail/IdeaPad_Slim_5_13ARP10?M=83J2003NIN" in md

    ctx = format_psref_llm_context(detail)
    assert "Official Lenovo PSREF Hardware Specifications" in ctx
    assert "16GB Soldered LPDDR5X-6400" in ctx

    # Platform Spec parsing for 4-char MT
    search_meta = parse_psref_search_summary(
        SAMPLE_SEARCH_TEXT,
        [
            {"text": "IdeaPad Slim 5 13ARP10", "href": "/Product/IdeaPad_Slim_5_13ARP10"},
            {"text": "Specifications", "href": "/Product/IdeaPad_Slim_5_13ARP10?tab=spec"},
        ],
    )
    assert search_meta["results_count"] == 1
    assert search_meta["product_name"] == "IdeaPad Slim 5 13ARP10"
    assert search_meta["total_models_hint"] == "236"
    assert len(search_meta["sample_models"]) == 1

    mt_spec = parse_psref_platform_spec_text(
        SAMPLE_PLATFORM_SPEC_TEXT,
        psref_url="https://psref.lenovo.com/Product/IdeaPad_Slim_5_13ARP10?tab=spec",
        requested_code="83J2",
        search_meta=search_meta,
    )
    assert mt_spec["found"] is True
    assert mt_spec["lookup_type"] == "mt"
    assert mt_spec["machine_type"] == "83J2"
    mt_md = format_psref_markdown(mt_spec)
    assert "236 total on PSREF" in mt_md
    assert "83J2001DSC" in mt_md


@pytest.mark.asyncio
async def test_psref_cache_tool_and_api_endpoint(monkeypatch):
    test_code = "99ZZ00TEST"
    db._execute_sync("DELETE FROM psref_cache WHERE code = ?", (test_code,))

    mock_payload = {
        "found": True,
        "lookup_type": "mtm",
        "requested_code": test_code,
        "model_code": test_code,
        "machine_type": "99ZZ",
        "product_name": "ThinkPad Test X1",
        "withdrawn": False,
        "psref_url": f"https://psref.lenovo.com/detail/ThinkPad_Test_X1?M={test_code}",
        "sections": [
            {
                "section": "PERFORMANCE",
                "items": [{"label": "Processor", "value": "Intel Core Ultra 7"}],
            }
        ],
        "sample_models": [],
        "total_models_hint": "",
    }
    mock_fetch = AsyncMock(return_value=mock_payload)
    monkeypatch.setattr(LenovoPSREFService, "_fetch_live_via_cdp", mock_fetch)

    try:
        # Tool registry lookup stores result in cache
        tool_res = await tool_registry.execute_tool("lookup_lenovo_psref", {"code": test_code})
        assert tool_res.status == "success"
        assert tool_res.result["found"] is True
        assert tool_res.result["product_name"] == "ThinkPad Test X1"
        assert mock_fetch.await_count == 1

        # Second call via FastAPI hits SQLite cache without invoking CDP again
        api_res = client.get(f"/api/psref/lookup?code={test_code}")
        assert api_res.status_code == 200
        body = api_res.json()
        assert body["found"] is True
        assert body["cached"] is True
        assert body["product_name"] == "ThinkPad Test X1"
        assert mock_fetch.await_count == 1

        recent_res = client.get("/api/psref/recent")
        assert recent_res.status_code == 200
        assert any(it["code"] == test_code for it in recent_res.json()["items"])
    finally:
        db._execute_sync("DELETE FROM psref_cache WHERE code = ?", (test_code,))


def test_streamlit_no_separate_psref_page_and_auto_mtm_lookup(monkeypatch):
    """Verifies PSREF is not a separate sidebar page and entering an MTM in chat automatically looks up PSREF."""
    test_mtm = "83J2003TST"
    test_mtm2 = "83J2004TST"
    for c in (test_mtm, test_mtm2):
        db._execute_sync("DELETE FROM psref_cache WHERE code = ?", (c,))
    existing_before = {
        r["id"]
        for r in db._fetchall_sync("SELECT id FROM conversations WHERE user_id = ?", ("achakrabort4",))
    }

    async def _fake_cdp(query: str, lookup_type: str):
        return parse_psref_detail_text(
            SAMPLE_DETAIL_TEXT.replace("83J2003NIN", query.upper()),
            psref_url=f"https://psref.lenovo.com/detail/IdeaPad_Slim_5_13ARP10?M={query.upper()}",
            requested_code=query.upper(),
        )

    mock_fetch = AsyncMock(side_effect=_fake_cdp)
    monkeypatch.setattr(LenovoPSREFService, "_fetch_live_via_cdp", mock_fetch)

    captured_system_prompts = []

    async def _fake_stream_detailed(*args, **kwargs):
        messages = kwargs.get("messages") if "messages" in kwargs else args[1]
        sys_msgs = [m["content"] for m in messages if m.get("role") == "system"]
        captured_system_prompts.append("\n".join(sys_msgs))
        yield {"thinking": "", "content": "IdeaPad Slim 5 13ARP10 uses AMD Ryzen 7 7735HS and 16GB LPDDR5X."}

    from app.services.ollama_service import ollama_service
    monkeypatch.setattr(ollama_service, "chat_stream_detailed", _fake_stream_detailed)

    app_path = Path(__file__).parent.parent.parent / "streamlit_app.py"
    try:
        at = AppTest.from_file(str(app_path.resolve()), default_timeout=30)
        at.session_state["authenticated_user"] = {
            "name": "Archiman Chakraborty",
            "username": "achakrabort4",
            "role": "User",
            "workspace_path": str(Path(__file__).parent.parent.parent / "workspace" / "achakrabort4"),
        }
        at.run()
        assert len(at.exception) == 0

        # No separate PSREF page in sidebar navigation and no /psref chip
        assert not any("PSREF" in opt for opt in at.sidebar.radio[0].options)
        assert not any(b.key == "chip_psref" for b in at.button)

        # 1. Standalone MTM in Chat automatically displays PSREF specs
        at.chat_input[0].set_value(test_mtm).run()
        assert len(at.exception) == 0
        msgs = at.session_state["messages"]
        assert len(msgs) >= 2
        last_reply = msgs[-1]["content"]
        assert "Lenovo PSREF Specifications" in last_reply
        assert test_mtm in last_reply
        assert "IdeaPad Slim 5 13ARP10" in last_reply
        assert "Lenovo Tips & Resources Citations" not in last_reply
        assert "Part Removal Video" not in last_reply

        # 2. Conversational sentence with an MTM automatically runs PSREF and grounds Ollama
        at.chat_input[0].set_value(f"check {test_mtm2}").run()
        assert len(at.exception) == 0
        assert mock_fetch.await_count >= 2
        assert any("IdeaPad Slim 5 13ARP10" in sp and test_mtm2 in sp for sp in captured_system_prompts)
    finally:
        for c in (test_mtm, test_mtm2):
            db._execute_sync("DELETE FROM psref_cache WHERE code = ?", (c,))
        after_ids = {
            r["id"]
            for r in db._fetchall_sync("SELECT id FROM conversations WHERE user_id = ?", ("achakrabort4",))
        }
        for cid in after_ids - existing_before:
            db._execute_sync("DELETE FROM messages WHERE conversation_id = ?", (cid,))
            db._execute_sync("DELETE FROM conversations WHERE id = ?", (cid,))
