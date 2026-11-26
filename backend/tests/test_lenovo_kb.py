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
        assert sync_res["sources_count"] == 4
        assert sync_res["raw_records_count"] == 1654
        assert sync_res["entries_count"] == 1515
        assert sync_res["deduplicated_removed"] == 139

        stats = svc.get_stats()
        assert stats["by_type"]["known_issue"] == 494
        assert stats["by_type"]["known_tip"] == 58
        assert stats["by_type"]["leap_tip"] == 139
        assert stats["by_type"]["tech_tip"] == 824
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

    # Regular User sees Lenovo Tips & Issues nav and /tips chip, NOT Admin sync button
    at_user = AppTest.from_file(str(app_path.resolve()), default_timeout=30)
    at_user.session_state["authenticated_user"] = {
        "name": "Archiman Chakraborty",
        "username": "achakrabort4",
        "role": "User",
        "workspace_path": str(Path(__file__).parent.parent.parent / "workspace" / "achakrabort4"),
    }
    at_user.run()
    assert len(at_user.exception) == 0
    assert any("/tips" in b.label for b in at_user.button)

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
