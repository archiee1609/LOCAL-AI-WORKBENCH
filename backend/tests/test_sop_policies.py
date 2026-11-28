from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from app.database.connection import db
from app.main import app
from app.services.sop_policy_service import sop_policy_service
from app.services.tool_registry import tool_registry

client = TestClient(app)


def test_sop_folder_sync_pdfs_and_image_ocr():
    """Verifies all 11 real SOPs & Policies files (10 PDFs + Battery Policy.png OCR) sync and rank accurately."""
    stats = sop_policy_service.sync_documents(force=False)
    assert stats["files"] >= 11
    assert stats["chunks"] >= 15

    battery_hits = sop_policy_service.search("battery policy warranty 12 months", limit=5)
    assert battery_hits
    assert any("Battery Policy.png" in h["source_file"] for h in battery_hits)
    img_hit = next(h for h in battery_hits if h["source_file"] == "Battery Policy.png")
    assert "12 months" in img_hit["content"].lower()
    assert img_hit["image_data_uri"].startswith("data:image/png;base64,")

    pixel_hits = sop_policy_service.search("dead pixel bright dark sub-pixel policy", limit=5)
    assert pixel_hits and any("Dead Pixel Policy" in h["source_file"] for h in pixel_hits)

    esc_hits = sop_policy_service.search("technical escalation handling L1.5 CEC L2", limit=5)
    assert esc_hits and any("Technical Escalation" in h["source_file"] for h in esc_hits)

    rps_hits = sop_policy_service.search("restricted party screening RPS sanctions", limit=5)
    assert rps_hits and any("Restricted Party Screening" in h["source_file"] for h in rps_hits)

    phonetic_hits = sop_policy_service.search("phonetic verification email id", limit=5)
    assert phonetic_hits and any("Phonetic Verification" in h["source_file"] for h in phonetic_hits)

    adp_hits = sop_policy_service.search("ADP accidental damage protection coverage", limit=5)
    assert adp_hits and any("ADP Policy" in h["source_file"] for h in adp_hits)


def test_sop_chat_intent_gating():
    """Verifies SOP lookup fires only for explicit SOP/process/policy requests or /sop commands."""
    assert sop_policy_service.detect_chat_sop_intent("What is Lenovo battery policy?")["show_sop"] is True
    assert sop_policy_service.detect_chat_sop_intent("What is Lenovo dead pixel policy?")["show_sop"] is True
    assert sop_policy_service.detect_chat_sop_intent("How does technical escalation handling SOP work?")["show_sop"] is True
    assert sop_policy_service.detect_chat_sop_intent("CEC call logging process for LFR and LES")["show_sop"] is True
    cmd = sop_policy_service.detect_chat_sop_intent("/sop warranty terms")
    assert cmd["show_sop"] is True and cmd["explicit"] is True

    # Must NOT fire on email drafts, KB tips, removal videos, or general tasks
    assert sop_policy_service.detect_chat_sop_intent("draft a mail to customer about screen replacement")["show_sop"] is False
    assert sop_policy_service.detect_chat_sop_intent("show known tips for LOQ 15IRX9")["show_sop"] is False
    assert sop_policy_service.detect_chat_sop_intent("show battery removal video for ThinkPad T14")["show_sop"] is False
    assert sop_policy_service.detect_chat_sop_intent("write a python quicksort script")["show_sop"] is False


@pytest.mark.asyncio
async def test_sop_tool_registry_and_fastapi_chat_gating():
    res = await tool_registry.execute_tool("search_sop_policies", {"query": "dead pixel policy"})
    assert res.status == "success"
    assert res.result["total_matches"] >= 1
    assert any("Dead Pixel Policy" in r["source_file"] for r in res.result["results"])

    from app.services import ollama_service as ollama_mod

    conv_res = client.post("/api/chat/conversations", json={"title": "SOP API Gate Test", "model": "gemma4:12b"})
    assert conv_res.status_code == 200
    conv_id = conv_res.json()["id"]

    async def _fake_stream(*args, **kwargs):
        yield {"message": {"content": "Verified SOP summary."}}

    try:
        with patch.object(ollama_mod.ollama_service, "stream_chat", side_effect=_fake_stream):
            r_mail = client.post(
                "/api/chat/completions",
                json={"conversation_id": conv_id, "message": "draft a mail to customer about battery delay", "enable_tools": False},
            )
            assert r_mail.status_code == 200
            assert '"type": "sop_results"' not in r_mail.text
            assert '"type": "kb_results"' not in r_mail.text

            r_sop = client.post(
                "/api/chat/completions",
                json={"conversation_id": conv_id, "message": "What is Lenovo dead pixel policy?", "enable_tools": False},
            )
            assert r_sop.status_code == 200
            assert '"type": "sop_results"' in r_sop.text
            assert "Lenovo Dead Pixel Policy" in r_sop.text
            assert '"type": "kb_results"' not in r_sop.text
    finally:
        client.delete(f"/api/chat/conversations/{conv_id}")


def test_streamlit_sop_no_extra_tab_and_chat_isolation(monkeypatch):
    """Verifies no extra SOP tab exists in navigation, SOP queries ground + cite cleanly, and non-SOP drafts stay clean."""
    existing_before = {
        r["id"]
        for r in db._fetchall_sync("SELECT id FROM conversations WHERE user_id = ?", ("achakrabort4",))
    }

    captured_sys = []

    async def _fake_stream_detailed(*args, **kwargs):
        messages = kwargs.get("messages") if "messages" in kwargs else args[1]
        captured_sys.append("\n".join(m["content"] for m in messages if m.get("role") == "system"))
        yield {"type": "content", "text": "Lenovo batteries and stylus pens carry a 12-month limited warranty."}

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

        # No extra SOP/Policies sidebar radio item
        assert not any("SOP" in opt or "Policy" in opt for opt in at.sidebar.radio[0].options)

        # 1. SOP / Policy question -> pulls Battery Policy.png OCR + image, zero unrequested KB tips/videos
        at.chat_input[0].set_value("What is Lenovo battery policy?").run()
        assert len(at.exception) == 0
        reply1 = at.session_state["messages"][-1]
        assert "SOP & Policy Citations" in reply1["content"]
        assert "Battery Policy.png" in reply1["content"]
        assert len(reply1.get("images") or []) >= 1
        assert "Lenovo Tips & Resources Citations" not in reply1["content"]
        assert "Part Removal Video" not in reply1["content"]
        assert any("12 months" in s.lower() for s in captured_sys)

        # 2. General email draft -> NO SOP citations, NO KB tips, NO videos, NO policy images
        at.chat_input[0].set_value("draft a mail to customer declining out-of-warranty repair").run()
        assert len(at.exception) == 0
        reply2 = at.session_state["messages"][-1]
        assert "SOP & Policy Citations" not in reply2["content"]
        assert "Lenovo Tips & Resources Citations" not in reply2["content"]
        assert "Part Removal Video" not in reply2["content"]
        assert not (reply2.get("images") or [])
    finally:
        after_ids = {
            r["id"]
            for r in db._fetchall_sync("SELECT id FROM conversations WHERE user_id = ?", ("achakrabort4",))
        }
        for cid in after_ids - existing_before:
            db._execute_sync("DELETE FROM messages WHERE conversation_id = ?", (cid,))
            db._execute_sync("DELETE FROM conversations WHERE id = ?", (cid,))
