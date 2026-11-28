"""
Automated Tests for Lenovo 'Tips & Resources' Knowledge Base, Auto-Sync, Search & UI.
"""

import shutil
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient
from streamlit.testing.v1 import AppTest

from app.database.connection import Database
from app.database.migrations import SCHEMA_SQL
from app.main import app
from app.services import lenovo_kb_service as kb_mod
from app.services.lenovo_kb_service import LenovoKBService
from app.services.tool_registry import tool_registry


def _make_isolated_kb(tmp_path: Path):
    iso_db = Database(db_path=tmp_path / "kb_test.db")
    iso_db._executescript_sync(SCHEMA_SQL)
    assets_dir = tmp_path / "kb_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    return iso_db, assets_dir


def test_real_workbooks_ingestion_dedup_links_and_images(tmp_path):
    iso_db, assets_dir = _make_isolated_kb(tmp_path)
    svc = LenovoKBService()
    kb_dir = Path(__file__).parent.parent.parent / "Tips & Resources"
    assert kb_dir.exists(), "Expected Tips & Resources folder at repo root"

    with patch.object(kb_mod, "db", iso_db):
        sync_res = svc.sync_knowledge_base(force=True, kb_dir=kb_dir, assets_dir=assets_dir)
        assert sync_res["synced"] is True
        assert sync_res["sources_count"] == 5
        assert sync_res["raw_records_count"] == 8074
        assert sync_res["entries_count"] == 2213
        assert sync_res["deduplicated_removed"] == 5861

        stats = svc.get_stats()
        assert stats["by_type"]["known_issue"] == 494
        assert stats["by_type"]["known_tip"] == 58
        assert stats["by_type"]["leap_tip"] == 139
        assert stats["by_type"]["tech_tip"] == 824
        assert stats["by_type"]["video_guide"] == 698
        assert stats["confirmed_qi_count"] == 376

        # Verify embedded SOP images extracted from Known_Tips 1.xlsx
        kt_source = next(s for s in stats["sources"] if "Known_Tips" in s["file_name"])
        assert kt_source["asset_count"] == 11
        extracted_files = list(assets_dir.rglob("*"))
        assert len([p for p in extracted_files if p.is_file()]) == 11

        # Fast incremental no-op check
        noop = svc.sync_knowledge_base(force=False, kb_dir=kb_dir, assets_dir=assets_dir)
        assert noop["synced"] is False
        assert noop["reason"] == "up_to_date"

        # Verify search precision, alias expansion, links & LLM context formatting
        assert svc.detect_lenovo_intent("Write a Python merge sort") is False
        assert svc.detect_lenovo_intent("21CB WWAN error") is True
        assert svc.detect_lenovo_intent("POST 2100 error") is True
        assert svc.detect_lenovo_intent("0302") is True
        assert svc.detect_lenovo_intent("10050") is True

        vid_hits = svc.search("ThinkPad T14 Gen 1 part removal video", limit=3)
        assert len(vid_hits) >= 1
        assert vid_hits[0]["source_type"] == "video_guide"
        assert "ThinkPad T14 Gen 1" in vid_hits[0]["title"]
        assert "20S0" in vid_hits[0]["machine_types"]
        assert "20S5" in vid_hits[0]["machine_types"]
        assert any("HT510512" in lk["url"] for lk in vid_hits[0]["links"])

        mt_num_hits = svc.search("0302", limit=2)
        assert len(mt_num_hits) >= 1
        assert mt_num_hits[0]["source_type"] == "video_guide"
        assert "Flex 2 Pro 15" in mt_num_hits[0]["model_names"]

        wwan_hits = svc.search("21CB WWAN", limit=3)
        assert len(wwan_hits) >= 1
        assert "FM350-GL" in wwan_hits[0]["title"]

        t14_hits = svc.search("ThinkPad T14 Gen 6 restart", limit=3)
        assert len(t14_hits) >= 1
        assert "T14 Gen 6" in t14_hits[0]["title"]
        assert len(t14_hits[0]["source_files"]) == 2  # Deduplicated across AP Services & LEAP Tips 1.xlsx

        ip3_hits = svc.search("IdeaPad 3 no post", limit=3)
        assert len(ip3_hits) >= 1
        assert "IP3" in ip3_hits[0]["title"] or "IP 3" in ip3_hits[0]["title"]

        post_hits = svc.search("POST 2100", limit=3)
        assert len(post_hits) >= 1
        assert any("support.lenovo.com" in lk["url"] for lk in post_hits[0]["links"])

        aptt_hits = svc.search("APTT0819", limit=2)
        assert len(aptt_hits) == 1
        assert aptt_hits[0]["issue_code"] == "APTT0819"
        assert any("bsharpcorp.com" in lk["url"] for lk in aptt_hits[0]["links"])

        ctx = svc.format_context_for_llm("21CB WWAN", wwan_hits)
        assert "[KB-1]" in ctx
        assert "Strict Grounding Rules" in ctx

    iso_db.close()


def test_periodic_updates_csv_and_markdown_auto_sync(tmp_path):
    iso_db, assets_dir = _make_isolated_kb(tmp_path)
    kb_dir = tmp_path / "Tips & Resources"
    kb_dir.mkdir()
    svc = LenovoKBService()

    # Copy Known_Tips 1.xlsx and add new periodic CSV + Markdown docs
    src_kt = Path(__file__).parent.parent.parent / "Tips & Resources" / "Known_Tips 1.xlsx"
    shutil.copy2(src_kt, kb_dir / "Known_Tips 1.xlsx")

    md_file = kb_dir / "q2_service_bulletin.md"
    md_file.write_text(
        "# ThinkPad Z16 Gen 2 USB4 Drop Bulletin\n"
        "Machine Type 21JX experiencing USB4 dock disconnect should update Thunderbolt retimer FW v1.42.",
        encoding="utf-8",
    )

    with patch.object(kb_mod, "db", iso_db):
        res1 = svc.sync_knowledge_base(force=False, kb_dir=kb_dir, assets_dir=assets_dir)
        assert res1["synced"] is True
        assert res1["sources_count"] == 2
        z16_hits = svc.search("21JX retimer FW", limit=3)
        assert len(z16_hits) >= 1
        assert "v1.42" in z16_hits[0]["resolution"]

        # Periodic update via save_uploaded_resource
        csv_bytes = (
            b"Model,Issue,Resolution,MT\n"
            b"Legion Slim 5,Fan pulsing after sleep,Update EC firmware KECN42WW,82YA\n"
        )
        up_res = svc.save_uploaded_resource("monthly_update.csv", csv_bytes, kb_dir=kb_dir)
        assert up_res["saved_file"] == "monthly_update.csv"
        legion_hits = svc.search("82YA KECN42WW", limit=2)
        assert len(legion_hits) == 1
        assert "KECN42WW" in legion_hits[0]["resolution"]

        # Removing a file purges deleted entries automatically on next sync
        md_file.unlink()
        res2 = svc.sync_knowledge_base(force=False, kb_dir=kb_dir, assets_dir=assets_dir)
        assert res2["synced"] is True
        assert svc.search("21JX retimer", limit=3) == []

    iso_db.close()


def test_kb_api_endpoints_rbac_and_tool_registry(tmp_path):
    client = TestClient(app)

    res_stats = client.get("/api/kb/stats")
    assert res_stats.status_code == 200
    assert res_stats.json()["total_entries"] >= 1500

    res_search = client.get("/api/kb/search?q=21CB&limit=5")
    assert res_search.status_code == 200
    data = res_search.json()
    assert data["total_matches"] >= 1
    assert any("21CB" in r["machine_types"] for r in data["results"])

    # Rule 1 RBAC: Non-admin blocked from sync and upload
    res_user_sync = client.post("/api/kb/sync?actor_role=User")
    assert res_user_sync.status_code == 403

    res_user_up = client.post(
        "/api/kb/upload?actor_role=User",
        files={"file": ("test.txt", b"hello", "text/plain")},
    )
    assert res_user_up.status_code == 403

    # Admin sync allowed
    res_admin_sync = client.post("/api/kb/sync?force=false&actor_role=Admin&actor_username=admin")
    assert res_admin_sync.status_code == 200

    # Verify search_lenovo_kb is registered in agent tool registry
    tool_names = [t.name for t in tool_registry.get_all_definitions()]
    assert "search_lenovo_kb" in tool_names


def test_streamlit_lenovo_kb_explorer_renders_cleanly():
    app_path = Path(__file__).parent.parent.parent / "streamlit_app.py"

    # Regular User sees Lenovo Tips & Issues nav and /tips hint in chat input, NOT Admin sync button
    at_user = AppTest.from_file(str(app_path.resolve()), default_timeout=30)
    at_user.session_state["authenticated_user"] = {
        "name": "Archiman Chakraborty",
        "username": "achakrabort4",
        "role": "User",
        "workspace_path": str(Path(__file__).parent.parent.parent / "workspace" / "achakrabort4"),
    }
    at_user.run()
    assert len(at_user.exception) == 0
    assert any("/tips" in (ci.placeholder or "") for ci in at_user.chat_input)

    user_nav = at_user.sidebar.radio[0].options
    assert any("Lenovo Tips & Issues" in opt for opt in user_nav)
    kb_opt = next(opt for opt in user_nav if "Lenovo Tips & Issues" in opt)
    at_user.sidebar.radio[0].set_value(kb_opt).run()
    assert len(at_user.exception) == 0
    assert any("Indexed KB Records" == m.label for m in at_user.metric)
    assert not any(b.key == "btn_force_kb_sync" for b in at_user.button)

    # Admin sees Lenovo KB Explorer AND Force Re-Sync Folder Now button
    at_admin = AppTest.from_file(str(app_path.resolve()), default_timeout=30)
    at_admin.session_state["authenticated_user"] = {
        "name": "Admin",
        "username": "admin",
        "role": "Admin",
        "workspace_path": str(Path(__file__).parent.parent.parent / "workspace" / "admin"),
    }
    at_admin.run()
    assert len(at_admin.exception) == 0
    at_admin.sidebar.radio[0].set_value(kb_opt).run()
    assert len(at_admin.exception) == 0
    assert any(b.key == "btn_force_kb_sync" for b in at_admin.button)


def test_resolve_solution_videos_redirects_extracts_ranks_and_caches():
    import json as _json
    from unittest.mock import MagicMock

    svc = LenovoKBService()

    html_wrapper = (
        "<html><head><title>Part Removal Video - ThinkPad T14 - Lenovo Support US</title></head>"
        "<body><script src='/us/en/api/v4/contents/cdn/ht510512_999000.js'></script></body></html>"
    )
    cdn_body_html = (
        "<p><a href='https://download.lenovo.com/pccbbs/mobiles_pdf/t14_gen1_hmm_en.pdf'>ThinkPad T14 HMM</a></p>"
        "<table>"
        "<tr><td><a href='https://download.lenovo.com/lts/RTPW10U0/FOF/RTPW10U0_DisableBattery.mp4'>Disable Built-In Battery</a></td></tr>"
        "<tr><td><a href='https://download.lenovo.com/lts/RTPW10U0/FOF/RTPW10U0_Keyboard.mp4'>Keyboard</a></td></tr>"
        "<tr><td><a href='https://download.lenovo.com/lts/RTPW10U0/FOF/RTPW10U0_BuiltInBattery.mp4'>Built-in Battery</a></td></tr>"
        "<tr><td><a href='https://download.lenovo.com/lts/RTPW10U0/FOF/RTPW10U0_M2SSD.mp4'>M.2 Solid State Drive</a></td></tr>"
        "<tr><td><a href='https://www.youtube.com/watch?v=abc123XYZ99'>Overview Clip</a></td></tr>"
        "</table>"
    )
    cdn_js = "window.cdnData = window.cdnData || {};Object.assign(window.cdnData," + _json.dumps({"body": cdn_body_html}) + ");"

    call_urls = []

    def _fake_get(url):
        call_urls.append(str(url))
        resp = MagicMock()
        resp.status_code = 200
        resp.url = str(url)
        if str(url).endswith(".js"):
            resp.text = cdn_js
        else:
            resp.text = html_wrapper
        return resp

    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.get.side_effect = _fake_get

    with patch("app.services.lenovo_kb_service.httpx.Client", return_value=fake_client):
        res_kbd = svc.resolve_solution_videos(
            "https://support.lenovo.com/us/en/solutions/HT510512",
            query_hint="ThinkPad T14 keyboard removal video",
        )
        assert res_kbd["solution_id"] == "HT510512"
        assert res_kbd["resolved_url"] == "https://pcsupport.lenovo.com/us/en/solutions/ht510512"
        assert call_urls[0] == "https://pcsupport.lenovo.com/us/en/solutions/ht510512"
        assert call_urls[1] == "https://pcsupport.lenovo.com/us/en/api/v4/contents/cdn/ht510512_999000.js"
        assert res_kbd["cached"] is False
        assert len(res_kbd["videos"]) == 5
        assert len(res_kbd["manuals"]) == 1
        assert res_kbd["recommended_video"]["title"] == "Keyboard"
        assert res_kbd["recommended_video"]["video_type"] == "mp4"

        # Second call hits in-memory cache and re-ranks dynamically for SSD
        res_ssd = svc.resolve_solution_videos(
            "https://support.lenovo.com/us/en/solutions/HT510512",
            query_hint="how to remove SSD",
        )
        assert res_ssd["cached"] is True
        assert len(call_urls) == 2  # No extra network call
        assert res_ssd["recommended_video"]["title"] == "M.2 Solid State Drive"

        # Battery query avoids Disable Built-In Battery and picks Built-in Battery
        res_bat = svc.resolve_solution_videos(
            "HT510512",
            query_hint="battery removal",
        )
        assert res_bat["recommended_video"]["title"] == "Built-in Battery"

    # Direct MP4 link bypasses scraping
    res_direct = svc.resolve_solution_videos("https://download.lenovo.com/lts/RTPW10U0_Keyboard.mp4")
    assert res_direct["recommended_video"]["video_type"] == "mp4"
    assert res_direct["recommended_video"]["url"].endswith("_Keyboard.mp4")

    # Graceful offline error
    with patch("app.services.lenovo_kb_service.httpx.Client", side_effect=RuntimeError("offline")):
        res_err = svc.resolve_solution_videos("HT516060", force_refresh=True)
        assert res_err["videos"] == []
        assert "offline" in res_err["error"]


def test_kb_resolve_videos_api_endpoint():
    client = TestClient(app)
    mock_payload = {
        "solution_id": "HT516060",
        "original_url": "https://support.lenovo.com/us/en/solutions/HT516060",
        "resolved_url": "https://pcsupport.lenovo.com/us/en/solutions/ht516060",
        "cdn_script_url": "https://pcsupport.lenovo.com/us/en/api/v4/contents/cdn/ht516060_1.js",
        "page_title": "Lenovo Flex 2 Pro 15",
        "videos": [
            {
                "title": "Battery Removal",
                "url": "https://download.lenovo.com/lts/RCNW1120/FOF/Battery.mp4",
                "video_type": "mp4",
                "action_type": "removal",
                "match_score": 11.5,
            }
        ],
        "recommended_video": {
            "title": "Battery Removal",
            "url": "https://download.lenovo.com/lts/RCNW1120/FOF/Battery.mp4",
            "video_type": "mp4",
            "action_type": "removal",
            "match_score": 11.5,
        },
        "best_index": 0,
        "manuals": [],
        "error": "",
        "cached": False,
    }
    with patch.object(kb_mod.lenovo_kb_service, "resolve_solution_videos", return_value=mock_payload) as mock_fn:
        res = client.get(
            "/api/kb/resolve-videos",
            params={"url": "https://support.lenovo.com/us/en/solutions/HT516060", "q": "battery removal"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["solution_id"] == "HT516060"
    assert body["recommended_video"]["title"] == "Battery Removal"
    mock_fn.assert_called_once()


def test_streamlit_chat_and_explorer_video_playback_persistence():
    app_path = Path(__file__).parent.parent.parent / "streamlit_app.py"
    mock_bundle = {
        "solution_id": "HT516060",
        "original_url": "https://support.lenovo.com/us/en/solutions/HT516060",
        "resolved_url": "https://pcsupport.lenovo.com/us/en/solutions/ht516060",
        "cdn_script_url": "",
        "page_title": "Part Removal Video: Lenovo Flex 2 Pro 15, Edge 15",
        "videos": [
            {
                "title": "Bottom Case Removal",
                "url": "https://download.lenovo.com/lts/RCNW1120/FOF/BottomCase.mp4",
                "video_type": "mp4",
                "action_type": "removal",
                "match_score": 3.5,
            },
            {
                "title": "Battery Removal",
                "url": "https://download.lenovo.com/lts/RCNW1120/FOF/Battery.mp4",
                "video_type": "mp4",
                "action_type": "removal",
                "match_score": 14.0,
            },
        ],
        "recommended_video": {
            "title": "Battery Removal",
            "url": "https://download.lenovo.com/lts/RCNW1120/FOF/Battery.mp4",
            "video_type": "mp4",
            "action_type": "removal",
            "match_score": 14.0,
        },
        "best_index": 1,
        "manuals": [{"title": "Flex 2 Pro 15 HMM", "url": "https://download.lenovo.com/hmm.pdf"}],
        "error": "",
        "cached": True,
    }

    with patch.object(kb_mod.lenovo_kb_service, "resolve_solution_videos", return_value=mock_bundle):
        at = AppTest.from_file(str(app_path.resolve()), default_timeout=30)
        at.session_state["authenticated_user"] = {
            "name": "Archiman Chakraborty",
            "username": "achakrabort4",
            "role": "User",
            "workspace_path": str(Path(__file__).parent.parent.parent / "workspace" / "achakrabort4"),
        }
        at.run()
        assert len(at.exception) == 0

        conv_id = at.session_state["active_conv_id"]
        at.session_state["messages"] = [
            {"role": "user", "content": "Flex 2 Pro 15 battery removal video"},
            {
                "role": "assistant",
                "content": (
                    "Here is the Battery Removal guide for Flex 2 Pro 15.\n\n"
                    '<!-- kb_video_meta:{"entry_title":"Part Removal Video: Lenovo Flex 2 Pro 15",'
                    '"solution_id":"HT516060","original_url":"https://support.lenovo.com/us/en/solutions/HT516060",'
                    '"resolved_url":"https://pcsupport.lenovo.com/us/en/solutions/ht516060",'
                    '"query_hint":"Flex 2 Pro 15 battery removal video"} -->'
                ),
            },
        ]
        at.session_state["autoplay_video_key"] = f"{conv_id}:1"
        at.run()
        assert len(at.exception) == 0, f"Chat video player raised exceptions: {[e.value for e in at.exception]}"
        # Auto-matched selectbox rendered for the persisted video message and autoplay token consumed
        expected_sel_key = f"chat_vid_{conv_id}_1_part_sel"
        assert any(sb.key == expected_sel_key and sb.value == 1 for sb in at.selectbox)
        assert any("Auto-Matched Video Player" in m.value for m in at.markdown)
        assert at.session_state["autoplay_video_key"] == ""

        # Verify KB Explorer search auto-resolves & renders part removal video selector cleanly
        kb_opt = next(opt for opt in at.sidebar.radio[0].options if "Lenovo Tips & Issues" in opt)
        at.sidebar.radio[0].set_value(kb_opt).run()
        assert len(at.exception) == 0
        search_inp = next(ti for ti in at.text_input if "Search" in ti.label or "Model" in ti.label)
        search_inp.set_value("Flex 2 Pro 15").run()
        assert len(at.exception) == 0
        assert any("_part_sel" in (sb.key or "") for sb in at.selectbox)


def test_chat_kb_intent_gating_no_unsolicited_tips_or_videos(tmp_path):
    """
    Known Tips and Part Removal Videos must ONLY appear in chat when explicitly asked for.
    Drafting emails, general questions, and non-KB tasks must never attach KB tips or videos.
    """
    iso_db, assets_dir = _make_isolated_kb(tmp_path)
    svc = LenovoKBService()
    kb_dir = Path(__file__).parent.parent.parent / "Tips & Resources"

    with patch.object(kb_mod, "db", iso_db):
        svc.sync_knowledge_base(force=False, kb_dir=kb_dir, assets_dir=assets_dir)

        # 1. Email drafting & general tasks (even with Lenovo model/issue/replace/number mentions) -> neither tips nor video
        for non_kb_prompt in [
            "draft a mail",
            "draft a mail to customer regarding LOQ 15IRX9",
            "draft an email about ThinkPad T14 keyboard replacement delay",
            "write an email apologizing for ThinkPad E14 freezing issue",
            "compose a reply mentioning 21CB WWAN and 100 units",
            "draft a mail with tips to keep the team aligned",
            "what are the specs of Lenovo LOQ 15IRX9?",
            "compare ThinkPad T14 and IdeaPad 5",
            "21CB WWAN error",
            "Write a Python merge sort",
            "give me tips for writing a good resume",
            "find a Python tutorial video",
        ]:
            res = svc.detect_chat_kb_intent(non_kb_prompt)
            assert res == {"show_tips": False, "show_video": False}, f"Unexpected KB trigger for: {non_kb_prompt!r} -> {res}"

        # 2. Explicit tips / KB requests (without asking for a video) -> tips ONLY, NO video
        for tips_prompt in [
            "/tips ThinkPad T14 Gen 6 auto restart issue",
            "/kb 21CB WWAN",
            "!tips LOQ 15IRX9",
            "show me known tips for LOQ 15IRX9",
            "show tips for ThinkPad T14",
            "any known issues on LOQ 15IRX9?",
            "search lenovo kb for POST 2100",
            "show me known tips for LOQ 15IRX9 keyboard replacement",
            "draft a mail and show me the known tips for LOQ 15IRX9",
        ]:
            res = svc.detect_chat_kb_intent(tips_prompt)
            assert res == {"show_tips": True, "show_video": False}, f"Expected tips-only for: {tips_prompt!r} -> {res}"

        # 3. Explicit video requests -> video ONLY, NO unrelated tips
        for vid_prompt in [
            "show me the part removal video for LOQ 15IRX9",
            "ThinkPad T14 Gen 1 part removal video",
            "LOQ 15IRX9 keyboard removal video",
            "battery removal video for Flex 2 Pro 15",
            "play SSD replacement video for ThinkPad T14",
            "/tips ThinkPad T14 Gen 1 part removal video",
            "https://support.lenovo.com/us/en/solutions/HT510512",
        ]:
            res = svc.detect_chat_kb_intent(vid_prompt)
            assert res == {"show_tips": False, "show_video": True}, f"Expected video-only for: {vid_prompt!r} -> {res}"

        # 4. Combined request -> both tips and video
        both_res = svc.detect_chat_kb_intent("show known tips and part removal video for LOQ 15IRX9")
        assert both_res == {"show_tips": True, "show_video": True}

    iso_db.close()


def test_resolve_video_and_history_extraction_respect_video_intent():
    """
    Verify resolve_chat_kb_videos does not trigger solely because a video_guide appears
    in search results, and saved Known Tips/email messages with HT links do not spawn a player.
    """
    from app.services.lenovo_kb_service import (
        extract_kb_video_meta_from_text,
        resolve_chat_kb_videos,
    )

    fake_matches = [
        {
            "source_type": "known_issue",
            "title": "LOQ 15IRX9 BIOS Issue",
            "links": [{"label": "Ref", "url": "https://support.lenovo.com/us/en/solutions/HT515879"}],
        },
        {
            "source_type": "video_guide",
            "title": "Part Removal Video: Lenovo LOQ 15IRX9",
            "links": [{"label": "Video", "url": "https://support.lenovo.com/us/en/solutions/HT515879"}],
        },
    ]

    # Without explicit video intent in chat, video_guide in matches must NOT resolve or play a video
    assert resolve_chat_kb_videos("show known tips for LOQ 15IRX9", fake_matches, require_video_intent=True) is None
    assert resolve_chat_kb_videos("LOQ 15IRX9 keyboard replacement tips", fake_matches, require_video_intent=True) is None

    # A drafted email or Known Tips citation containing a Lenovo HT link must NOT spawn a video player on rerun
    email_text = "Subject: Update\n\nPlease review https://pcsupport.lenovo.com/us/en/solutions/ht515879 for details."
    assert extract_kb_video_meta_from_text(email_text, user_prompt="draft a mail about LOQ 15IRX9") is None
    tips_text = (
        "Here are the known tips.\n\n---\n"
        "**📚 Lenovo Tips & Resources Citations (1 match(es)):**\n"
        "- **[KB-1]** LOQ 15IRX9 BIOS Issue — [Open Reference](https://pcsupport.lenovo.com/us/en/solutions/ht515879)"
    )
    assert extract_kb_video_meta_from_text(tips_text, user_prompt="show known tips for LOQ 15IRX9") is None

    # Only persisted kb_video_meta or explicit video request restores the video player
    assert extract_kb_video_meta_from_text(email_text, user_prompt="show part removal video for LOQ 15IRX9") is not None


def _create_isolated_conv(username: str = "achakrabort4", model: str = "gemma4:12b") -> str:
    import datetime, uuid
    import streamlit as st
    from app.database.connection import db
    conv_id = str(uuid.uuid4())
    now = datetime.datetime.now().isoformat()
    db._execute_sync(
        """
        INSERT INTO conversations (id, title, model, system_prompt, temperature, context_length, created_at, updated_at, user_id)
        VALUES (?, ?, ?, '', 0.7, 8192, ?, ?, ?)
        """,
        (conv_id, "KB Gate Test", model, now, now, username),
    )
    st.cache_data.clear()
    return conv_id


def _cleanup_test_conv(conv_id):
    if not conv_id:
        return
    try:
        from app.database.connection import db
        db._execute_sync("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
        db._execute_sync("DELETE FROM conversations WHERE id = ?", (conv_id,))
    except Exception:
        pass


def test_streamlit_chat_draft_mail_shows_no_tips_or_video(tmp_path):
    """End-to-end Streamlit chat test: drafting a mail about LOQ 15IRX9 shows ONLY the drafted mail."""
    from app.services import ollama_service as ollama_mod

    app_path = Path(__file__).parent.parent.parent / "streamlit_app.py"

    async def _fake_stream_detailed(*args, **kwargs):
        yield {"type": "content", "text": "Subject: LOQ 15IRX9 Status Update\n\nDear Customer,\nYour LOQ 15IRX9 unit update is ready."}

    conv_id = _create_isolated_conv("achakrabort4")
    try:
        with patch.object(ollama_mod.ollama_service, "chat_stream_detailed", side_effect=_fake_stream_detailed):
            at = AppTest.from_file(str(app_path.resolve()), default_timeout=30)
            at.session_state["authenticated_user"] = {
                "name": "Archiman Chakraborty",
                "username": "achakrabort4",
                "role": "User",
                "workspace_path": str(Path(__file__).parent.parent.parent / "workspace" / "achakrabort4"),
            }
            at.query_params["conv"] = conv_id
            at.run()
            assert len(at.exception) == 0
            assert at.session_state["active_conv_id"] == conv_id

            # Submit a request to draft an email mentioning LOQ 15IRX9
            at.session_state["prompt_input"] = "draft a mail to customer regarding Lenovo LOQ 15IRX9"
            at.run()
            assert len(at.exception) == 0

            last_msg = at.session_state["messages"][-1]
            assert last_msg["role"] == "assistant"
            assert "LOQ 15IRX9 Status Update" in last_msg["content"]
            assert "Lenovo Tips & Resources Citations" not in last_msg["content"]
            assert "Redirected Video Stream" not in last_msg["content"]
            assert "kb_video_meta" not in last_msg["content"]
            assert last_msg.get("images", []) == []

            all_md = "\n".join(m.value for m in at.markdown)
            assert "LOQ 15IRX9 Status Update" in all_md
            assert "Lenovo Tips & Resources Citations" not in all_md
            assert "Redirected Video Stream" not in all_md
            assert "Auto-Matched Video Player" not in all_md
            assert not any("_part_sel" in (sb.key or "") for sb in at.selectbox)
    finally:
        _cleanup_test_conv(conv_id)


def test_streamlit_chat_tips_vs_video_separation(tmp_path):
    """End-to-end Streamlit chat test: asking for tips shows tips only; asking for video shows video only."""
    from app.services import ollama_service as ollama_mod

    app_path = Path(__file__).parent.parent.parent / "streamlit_app.py"
    mock_bundle = {
        "solution_id": "HT515879",
        "original_url": "https://support.lenovo.com/us/en/solutions/HT515879",
        "resolved_url": "https://pcsupport.lenovo.com/us/en/solutions/ht515879",
        "cdn_script_url": "",
        "page_title": "Part Removal Video: Lenovo LOQ 15IRX9",
        "videos": [
            {
                "title": "KeyboardBezel/LCD Assembly",
                "url": "https://download.lenovo.com/lts/MCNW2930/FOF/KeyboardBezel.mp4",
                "video_type": "mp4",
                "action_type": "guide",
                "match_score": 10.0,
            }
        ],
        "recommended_video": {
            "title": "KeyboardBezel/LCD Assembly",
            "url": "https://download.lenovo.com/lts/MCNW2930/FOF/KeyboardBezel.mp4",
            "video_type": "mp4",
            "action_type": "guide",
            "match_score": 10.0,
        },
        "best_index": 0,
        "manuals": [],
        "error": "",
        "cached": True,
    }

    async def _fake_stream_tips(*args, **kwargs):
        yield {"type": "content", "text": "Here are the known tips for LOQ 15IRX9."}

    conv_id1 = _create_isolated_conv("achakrabort4")
    conv_id2 = _create_isolated_conv("achakrabort4")
    try:
        # 1. Tips-only request -> Known Tips citations shown, NO video player
        with patch.object(ollama_mod.ollama_service, "chat_stream_detailed", side_effect=_fake_stream_tips), \
             patch.object(kb_mod.lenovo_kb_service, "resolve_solution_videos", return_value=mock_bundle) as mock_vid:
            at = AppTest.from_file(str(app_path.resolve()), default_timeout=30)
            at.session_state["authenticated_user"] = {
                "name": "Archiman Chakraborty",
                "username": "achakrabort4",
                "role": "User",
                "workspace_path": str(Path(__file__).parent.parent.parent / "workspace" / "achakrabort4"),
            }
            at.query_params["conv"] = conv_id1
            at.run()
            assert at.session_state["active_conv_id"] == conv_id1
            at.session_state["prompt_input"] = "show me known tips for LOQ 15IRX9"
            at.run()
            assert len(at.exception) == 0
            mock_vid.assert_not_called()
            all_md = "\n".join(m.value for m in at.markdown)
            assert "Lenovo Tips & Resources Citations" in all_md
            assert "Part Removal Video" not in all_md
            assert "Auto-Matched Video Player" not in all_md

        async def _fake_stream_video(*args, **kwargs):
            yield {"type": "content", "text": "Here is the LOQ 15IRX9 part removal video."}

        # 2. Video-only request -> Video player shown, NO Known Tips citations list
        with patch.object(ollama_mod.ollama_service, "chat_stream_detailed", side_effect=_fake_stream_video), \
             patch.object(kb_mod.lenovo_kb_service, "resolve_solution_videos", return_value=mock_bundle):
            at2 = AppTest.from_file(str(app_path.resolve()), default_timeout=30)
            at2.session_state["authenticated_user"] = {
                "name": "Archiman Chakraborty",
                "username": "achakrabort4",
                "role": "User",
                "workspace_path": str(Path(__file__).parent.parent.parent / "workspace" / "achakrabort4"),
            }
            at2.query_params["conv"] = conv_id2
            at2.run()
            assert at2.session_state["active_conv_id"] == conv_id2
            at2.session_state["prompt_input"] = "show me the part removal video for LOQ 15IRX9"
            at2.run()
            assert len(at2.exception) == 0
            all_md2 = "\n".join(m.value for m in at2.markdown)
            assert "Redirected Video Stream" in all_md2
            assert "Auto-Matched Video Player" in all_md2
            assert "Lenovo Tips & Resources Citations" not in all_md2
    finally:
        _cleanup_test_conv(conv_id1)
        _cleanup_test_conv(conv_id2)


def test_fastapi_chat_completions_respects_kb_gating():
    """Verify FastAPI SSE /api/chat/completions only emits kb_results when user asks for tips or video."""
    from unittest.mock import AsyncMock
    from app.services import ollama_service as ollama_mod

    client = TestClient(app)
    conv_res = client.post("/api/chat/conversations", json={"title": "KB Gate Test", "model": "gemma4:12b"})
    assert conv_res.status_code == 200
    conv_id = conv_res.json()["id"]

    async def _fake_stream_chat(*args, **kwargs):
        yield {"message": {"content": "Drafted email content."}}

    try:
        with patch.object(ollama_mod.ollama_service, "stream_chat", side_effect=_fake_stream_chat):
            # 1. Draft mail -> NO kb_results SSE event
            res_mail = client.post(
                "/api/chat/completions",
                json={"conversation_id": conv_id, "message": "draft a mail about LOQ 15IRX9", "enable_tools": False},
            )
            assert res_mail.status_code == 200
            assert '"type": "kb_results"' not in res_mail.text

            # 2. Ask for known tips -> emits kb_results SSE event
            res_tips = client.post(
                "/api/chat/completions",
                json={"conversation_id": conv_id, "message": "show known tips for LOQ 15IRX9", "enable_tools": False},
            )
            assert res_tips.status_code == 200
            assert '"type": "kb_results"' in res_tips.text
    finally:
        client.delete(f"/api/chat/conversations/{conv_id}")
