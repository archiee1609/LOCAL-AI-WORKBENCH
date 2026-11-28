from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from app.database.connection import db
from app.main import app
from app.services.context_router import build_context_scope_note, select_relevant_chat_history
from app.services.psref_service import LenovoPSREFService, parse_psref_detail_text

client = TestClient(app)


def test_unrelated_email_ignores_previous_model_search():
    prior = [
        {"role": "user", "content": "83J2003NIN"},
        {
            "role": "assistant",
            "content": (
                "### 📋 Lenovo PSREF Specifications — IdeaPad Slim 5 13ARP10\n"
                "Processor: AMD Ryzen 7 7735HS • RAM: 16GB LPDDR5X\n\n---\n**📋 Official PSREF Source:** link"
            ),
        },
    ]

    # Completely different context email draft -> excludes previous model search
    res = select_relevant_chat_history(
        "Draft a mail to customer requesting invoice copy for pickup",
        prior,
    )
    assert res["topic_shifted"] is True
    assert res["excluded_turns"] == 1
    assert res["messages"] == []
    assert "CURRENT user request" in build_context_scope_note(res["topic_shifted"])


def test_explicit_followup_keeps_previous_search():
    prior = [
        {"role": "user", "content": "83J2003NIN"},
        {
            "role": "assistant",
            "content": (
                "### 📋 Lenovo PSREF Specifications — IdeaPad Slim 5 13ARP10\n"
                "Processor: AMD Ryzen 7 7735HS\n\n---\n**📋 Official PSREF Source:** [PSREF](https://psref.lenovo.com/)"
            ),
        },
    ]

    res = select_relevant_chat_history(
        "Based on that, draft a mail to customer confirming its processor specs",
        prior,
    )
    assert res["topic_shifted"] is False
    assert len(res["messages"]) == 2
    assert "AMD Ryzen 7 7735HS" in res["messages"][1]["content"]
    # Citation trailer stripped when feeding back into model context
    assert "Official PSREF Source" not in res["messages"][1]["content"]


def test_multi_search_session_selective_recall_and_model_switch():
    prior = [
        {"role": "user", "content": "83J2003NIN"},
        {"role": "assistant", "content": "IdeaPad Slim 5 13ARP10 uses AMD Ryzen 7 7735HS, Weight 1.15 kg."},
        {"role": "user", "content": "What is Lenovo Dead Pixel Policy?"},
        {"role": "assistant", "content": "Lenovo Dead Pixel Policy covers bright and dark pixel thresholds."},
        {"role": "user", "content": "Draft a mail to HR for 2 days leave"},
        {"role": "assistant", "content": "Subject: Leave Request\nDear HR, I would like to apply for 2 days leave."},
    ]

    # New query about an earlier searched model -> keeps ONLY Turn 1 (IdeaPad Slim 5), skips unrelated HR/pixel turns
    res_revisit = select_relevant_chat_history(
        "What is the weight of the IdeaPad Slim 5 we checked earlier?",
        prior,
    )
    assert len(res_revisit["messages"]) == 2
    assert "IdeaPad Slim 5" in res_revisit["messages"][1]["content"]
    assert not any("HR" in m["content"] for m in res_revisit["messages"])

    # Switching to a different model -> excludes IdeaPad Slim 5 turn
    res_other_model = select_relevant_chat_history(
        "What are the specs for ThinkPad T14 21KC?",
        prior,
        has_fresh_context=True,
    )
    assert res_other_model["topic_shifted"] is True
    assert res_other_model["messages"] == []

    # Refining previous email draft -> keeps HR leave turn cleanly
    res_refine = select_relevant_chat_history("Make it shorter and more polite", prior)
    assert len(res_refine["messages"]) == 2
    assert "Leave Request" in res_refine["messages"][1]["content"]


def test_fastapi_chat_completions_isolates_unrelated_followup_mail():
    from app.services import ollama_service as ollama_mod

    conv_res = client.post("/api/chat/conversations", json={"title": "Topic Isolation Test", "model": "gemma4:12b"})
    assert conv_res.status_code == 200
    conv_id = conv_res.json()["id"]

    recorded_payloads = []

    async def _fake_stream_chat(*args, **kwargs):
        msgs = kwargs.get("messages") if "messages" in kwargs else args[1]
        recorded_payloads.append(list(msgs))
        yield {"message": {"content": "Mocked response for test."}}

    try:
        with patch.object(ollama_mod.ollama_service, "stream_chat", side_effect=_fake_stream_chat):
            # Turn 1: KB search on LOQ 15IRX9
            r1 = client.post(
                "/api/chat/completions",
                json={"conversation_id": conv_id, "message": "show known tips for LOQ 15IRX9", "enable_tools": False},
            )
            assert r1.status_code == 200

            # Turn 2: Draft unrelated email -> LOQ 15IRX9 turn must NOT appear in LLM messages
            r2 = client.post(
                "/api/chat/completions",
                json={"conversation_id": conv_id, "message": "Draft a mail to HR requesting sick leave tomorrow", "enable_tools": False},
            )
            assert r2.status_code == 200
            turn2_msgs = recorded_payloads[-1]
            turn2_non_sys = "\n".join(m["content"] for m in turn2_msgs if m["role"] != "system")
            assert "LOQ 15IRX9" not in turn2_non_sys
            assert any("Current Request Scope" in m["content"] for m in turn2_msgs if m["role"] == "system")
    finally:
        client.delete(f"/api/chat/conversations/{conv_id}")


def test_streamlit_chat_multi_topic_isolation(monkeypatch):
    """Verifies in Streamlit UI that an unrelated email draft ignores earlier model PSREF results while follow-up edit keeps current draft."""
    test_mtm = "83J2009TST"
    db._execute_sync("DELETE FROM psref_cache WHERE code = ?", (test_mtm,))
    existing_before = {
        r["id"]
        for r in db._fetchall_sync("SELECT id FROM conversations WHERE user_id = ?", ("achakrabort4",))
    }

    mock_payload = parse_psref_detail_text(
        "IdeaPad Slim 5 13ARP10\nModel: 83J2009TST\nPERFORMANCE\nProcessor\t\nAMD Ryzen 7 7735HS\nNote :",
        psref_url=f"https://psref.lenovo.com/detail/IdeaPad_Slim_5_13ARP10?M={test_mtm}",
        requested_code=test_mtm,
    )
    monkeypatch.setattr(LenovoPSREFService, "_fetch_live_via_cdp", AsyncMock(return_value=mock_payload))

    captured_calls = []

    async def _fake_stream_detailed(*args, **kwargs):
        msgs = kwargs.get("messages") if "messages" in kwargs else args[1]
        captured_calls.append(list(msgs))
        yield {"type": "content", "text": "Subject: Team Meeting Update\n\nHi team, tomorrow's sync moves to 3 PM."}

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

        # 1. Search a model MTM in chat
        at.chat_input[0].set_value(test_mtm).run()
        assert len(at.exception) == 0

        # 2. Draft an unrelated email in the exact same chat
        at.chat_input[0].set_value("Draft a mail to my team about rescheduling tomorrow's sync to 3 PM").run()
        assert len(at.exception) == 0
        call_msgs = captured_calls[-1]
        history_text = "\n".join(m["content"] for m in call_msgs if m["role"] != "system")
        assert test_mtm not in history_text
        assert "IdeaPad Slim 5" not in history_text

        # 3. Follow-up refinement of that email keeps Turn 2's email, NOT Turn 1's laptop model
        at.chat_input[0].set_value("Make it more formal").run()
        assert len(at.exception) == 0
        call_msgs_3 = captured_calls[-1]
        history_text_3 = "\n".join(m["content"] for m in call_msgs_3 if m["role"] != "system")
        assert "Team Meeting Update" in history_text_3
        assert test_mtm not in history_text_3
    finally:
        db._execute_sync("DELETE FROM psref_cache WHERE code = ?", (test_mtm,))
        after_ids = {
            r["id"]
            for r in db._fetchall_sync("SELECT id FROM conversations WHERE user_id = ?", ("achakrabort4",))
        }
        for cid in after_ids - existing_before:
            db._execute_sync("DELETE FROM messages WHERE conversation_id = ?", (cid,))
            db._execute_sync("DELETE FROM conversations WHERE id = ?", (cid,))
