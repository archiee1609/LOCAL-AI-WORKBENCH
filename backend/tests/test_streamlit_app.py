import sys
from pathlib import Path
from streamlit.testing.v1 import AppTest


def test_streamlit_unauthenticated_shows_login_portal():
    """Headless Streamlit UI test verifying that unauthenticated sessions render the Login Portal."""
    app_path = Path(__file__).parent.parent.parent / "streamlit_app.py"
    at = AppTest.from_file(str(app_path.resolve()), default_timeout=30)
    at.run()
    assert len(at.exception) == 0, f"AppTest raised unexpected exceptions: {[e.value for e in at.exception]}"
    # Verify login portal elements are present
    login_titles = [m.value for m in at.markdown if "Account Sign In" in m.value or "ISOTOPE" in m.value]
    assert len(login_titles) > 0
    # Username and Password fields are rendered
    assert len(at.text_input) >= 2
    # Governance rules & registered accounts list must NOT be exposed on login portal
    assert not any("Registered User Accounts:" in m.value for m in at.markdown)


def test_streamlit_app_renders_when_authenticated():
    """Headless Streamlit UI test verifying zero exceptions when user is authenticated."""
    from app.database.connection import db
    existing_before = {
        r["id"]
        for r in db._fetchall_sync("SELECT id FROM conversations WHERE user_id = ?", ("achakrabort4",))
    }
    app_path = Path(__file__).parent.parent.parent / "streamlit_app.py"
    at = AppTest.from_file(str(app_path.resolve()), default_timeout=30)
    at.session_state["authenticated_user"] = {
        "name": "Archiman Chakraborty",
        "username": "achakrabort4",
        "role": "User",
        "workspace_path": str(Path(__file__).parent.parent.parent / "workspace" / "achakrabort4")
    }
    at.run()
    assert len(at.exception) == 0, f"AppTest raised unexpected exceptions: {[e.value for e in at.exception]}"
    assert len(at.chat_input) > 0
    assert "messages" in at.session_state
    assert "active_conv_id" in at.session_state

    # Verify top brand banner renders cleanly without raw unparsed code blocks
    banner_markdowns = [m.value for m in at.markdown if '<div class="brand-container">' in m.value]
    assert len(banner_markdowns) > 0
    assert "ISOTOPE - AI COMPANION" in banner_markdowns[0]
    assert "100% Private" in banner_markdowns[0]
    # Ensure no raw unclosed or pre-formatted code block escapes
    assert "```" not in banner_markdowns[0]

    # Verify Chat History was moved out of sidebar into top-right header dropdown, and Options popover was removed
    assert not any("Recent Chats" in m.value for m in at.sidebar.markdown)
    assert any(ti.key == "header_chat_history_filter" for ti in at.text_input)
    assert any(b.key == "header_new_chat_btn" for b in at.button)
    assert not any(ti.key == "rename_chat_input" for ti in at.text_input)

    # Verify clicking "New Chat" in Recent Chats popover creates and switches to a new conversation
    prev_conv_id = at.session_state["active_conv_id"]
    new_btn = next(b for b in at.button if b.key == "header_new_chat_btn")
    new_btn.click().run()
    assert len(at.exception) == 0
    new_conv_id = at.session_state["active_conv_id"]
    assert new_conv_id != prev_conv_id
    assert at.session_state["messages"] == []

    # Verify pinning an older conversation moves it to the top of Recent Chats, and unpinning restores it
    pin_prev = next(b for b in at.button if b.key == f"pin_{prev_conv_id}")
    pin_prev.click().run()
    assert len(at.exception) == 0
    pinned_row = db._fetchone_sync("SELECT is_pinned FROM conversations WHERE id = ?", (prev_conv_id,))
    assert pinned_row and pinned_row["is_pinned"] == 1
    chat_btn_keys = [b.key for b in at.button if (b.key or "").startswith("ch_")]
    assert chat_btn_keys.index(f"ch_{prev_conv_id}") < chat_btn_keys.index(f"ch_{new_conv_id}")

    unpin_prev = next(b for b in at.button if b.key == f"pin_{prev_conv_id}")
    unpin_prev.click().run()
    assert len(at.exception) == 0
    unpinned_row = db._fetchone_sync("SELECT is_pinned FROM conversations WHERE id = ?", (prev_conv_id,))
    assert unpinned_row and unpinned_row["is_pinned"] == 0

    # Verify inline rename updates both SQLite and chat header without exposing rename_chat_input by default
    ren_new = next(b for b in at.button if b.key == f"ren_{new_conv_id}")
    ren_new.click().run()
    assert len(at.exception) == 0
    rename_ti = next(ti for ti in at.text_input if ti.key == f"rename_input_{new_conv_id}")
    rename_ti.input("Quarterly Review Notes").run()
    save_ren = next(b for b in at.button if b.key == f"save_ren_{new_conv_id}")
    save_ren.click().run()
    assert len(at.exception) == 0
    assert not any(ti.key == f"rename_input_{new_conv_id}" for ti in at.text_input)
    renamed_row = db._fetchone_sync("SELECT title FROM conversations WHERE id = ?", (new_conv_id,))
    assert renamed_row and renamed_row["title"] == "Quarterly Review Notes"
    assert any("Quarterly Review Notes" in m.value for m in at.markdown)

    # Clean up only conversations created by this test
    for cid in (prev_conv_id, new_conv_id):
        if cid not in existing_before:
            db._execute_sync("DELETE FROM messages WHERE conversation_id = ?", (cid,))
            db._execute_sync("DELETE FROM conversations WHERE id = ?", (cid,))


def test_streamlit_settings_page_renders_cleanly():
    """Headless test verifying Settings lives under Admin Account Management tab and is hidden for non-admin users."""
    app_path = Path(__file__).parent.parent.parent / "streamlit_app.py"

    # 1. Regular User must NOT see Settings in sidebar navigation
    at_user = AppTest.from_file(str(app_path.resolve()), default_timeout=30)
    at_user.session_state["authenticated_user"] = {
        "name": "Archiman Chakraborty",
        "username": "achakrabort4",
        "role": "User",
        "workspace_path": str(Path(__file__).parent.parent.parent / "workspace" / "achakrabort4")
    }
    at_user.run()
    assert len(at_user.exception) == 0
    if len(at_user.sidebar.radio) > 0:
        user_opts = at_user.sidebar.radio[0].options
        assert not any("Settings" in opt for opt in user_opts)

    # 2. Admin sees Settings inside Account Management tabs and Hardware & Acceleration renders cleanly
    at_admin = AppTest.from_file(str(app_path.resolve()), default_timeout=30)
    at_admin.session_state["authenticated_user"] = {
        "name": "Admin",
        "username": "admin",
        "role": "Admin",
        "workspace_path": str(Path(__file__).parent.parent.parent / "workspace" / "admin")
    }
    at_admin.run()
    assert len(at_admin.exception) == 0
    admin_opts = at_admin.sidebar.radio[0].options
    assert not any("Settings" in opt for opt in admin_opts)

    acc_opt = next(opt for opt in admin_opts if "Account Management" in opt)
    at_admin.sidebar.radio[0].set_value(acc_opt).run()
    assert len(at_admin.exception) == 0
    settings_markdowns = [m.value for m in at_admin.markdown if "Settings" in m.value or "Engines & Diagnostics" in m.value]
    assert len(settings_markdowns) > 0
    assert any("Registered User Accounts:" in m.value for m in at_admin.markdown)


def test_render_winxp_progress_bar_downloading_and_complete():
    """Verify Windows XP style progress bar renders Downloading state and Complete with Green Tick."""
    root_dir = str(Path(__file__).parent.parent.parent)
    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)
    from app.core.ui_components import render_winxp_progress_bar

    # 1. Downloading state
    html_dl = render_winxp_progress_bar(
        percent=45.5,
        status="downloading",
        detail="downloading layer 8a37e96 (450 MB / 1000 MB)"
    )
    assert "Status:" in html_dl
    assert "Downloading..." in html_dl
    assert "45.5%" in html_dl
    assert "repeating-linear-gradient" in html_dl  # XP segmented bricks
    assert "downloading layer 8a37e96" in html_dl

    # 2. Complete state with Green Tick
    html_comp = render_winxp_progress_bar(
        percent=100.0,
        status="complete",
        detail="Model ready"
    )
    assert "Status:" in html_comp
    assert "Complete" in html_comp
    assert "✔" in html_comp  # Green Tick
    assert "100.0%" in html_comp
    assert "width:100.0%" in html_comp

    # 3. Stopped / Paused state
    html_stop = render_winxp_progress_bar(
        percent=62.5,
        status="stopped",
        detail="Cancelled by user"
    )
    assert "Status:" in html_stop
    assert "Stopped" in html_stop
    assert "⏹" in html_stop
    assert "62.5%" in html_stop


def test_streamlit_quick_command_chips_render_cleanly():
    """Headless test verifying the quick command shortcut chips render cleanly above the chat input."""
    app_path = Path(__file__).parent.parent.parent / "streamlit_app.py"
    at = AppTest.from_file(str(app_path.resolve()), default_timeout=30)
    at.session_state["authenticated_user"] = {
        "name": "Archiman Chakraborty",
        "username": "achakrabort4",
        "role": "User",
        "workspace_path": str(Path(__file__).parent.parent.parent / "workspace" / "achakrabort4")
    }
    at.run()
    assert len(at.exception) == 0

    # Verify buttons for fast command chips exist
    btn_labels = [b.label for b in at.button]
    assert any("/code" in lbl for lbl in btn_labels)
    assert any("/report" in lbl for lbl in btn_labels)
    assert any("/search" in lbl for lbl in btn_labels)
    assert any("/pull" in lbl for lbl in btn_labels)

    # Verify model selector exists directly in the chat command toolbar
    model_selects = [s for s in at.selectbox if s.key == "chat_model_selector"]
    assert len(model_selects) == 1
    assert model_selects[0].value in model_selects[0].options


def test_streamlit_chatbox_model_selector_switch():
    """Headless test verifying model selection directly at the chatbox toolbar updates state."""
    app_path = Path(__file__).parent.parent.parent / "streamlit_app.py"
    at = AppTest.from_file(str(app_path.resolve()), default_timeout=30)
    at.session_state["authenticated_user"] = {
        "name": "Archiman Chakraborty",
        "username": "achakrabort4",
        "role": "User",
        "workspace_path": str(Path(__file__).parent.parent.parent / "workspace" / "achakrabort4")
    }
    at.run()
    assert len(at.exception) == 0

    model_selects = [s for s in at.selectbox if s.key == "chat_model_selector"]
    assert len(model_selects) == 1
    opts = model_selects[0].options
    if len(opts) > 1:
        target_opt = opts[1] if opts[0] == model_selects[0].value else opts[0]
        model_selects[0].select(target_opt).run()
        assert len(at.exception) == 0
        assert at.session_state.get("selected_model_val") == target_opt


def test_streamlit_account_management_admin_page_renders_cleanly():
    """Headless test verifying Account Management studio renders cleanly for Admin and is hidden for User."""
    app_path = Path(__file__).parent.parent.parent / "streamlit_app.py"
    at = AppTest.from_file(str(app_path.resolve()), default_timeout=30)
    # 1. Admin login simulation
    at.session_state["authenticated_user"] = {
        "name": "Admin",
        "username": "admin",
        "role": "Admin",
        "workspace_path": str(Path(__file__).parent.parent.parent / "workspace" / "admin")
    }
    at.run()
    assert len(at.exception) == 0
    assert len(at.sidebar.radio) > 0
    # Rule 1: Admin sees Account Management in navigation
    radio_options = at.sidebar.radio[0].options
    assert any("Account Management" in opt for opt in radio_options)

    # Select Account Management
    acc_opt = next(opt for opt in radio_options if "Account Management" in opt)
    at.sidebar.radio[0].set_value(acc_opt).run()
    assert len(at.exception) == 0
    acc_markdowns = [m.value for m in at.markdown if "Account Management & Load Balancer" in m.value]
    assert len(acc_markdowns) > 0
    # Verify Admin password reset control is present in User Accounts tab
    assert any(t.key == "upd_pwd_new" for t in at.text_input)
    assert any(s.key == "upd_pwd_target_user" for s in at.selectbox)

    # 2. Non-Admin simulation
    at_user = AppTest.from_file(str(app_path.resolve()), default_timeout=30)
    at_user.session_state["authenticated_user"] = {
        "name": "Archiman Chakraborty",
        "username": "achakrabort4",
        "role": "User",
        "workspace_path": str(Path(__file__).parent.parent.parent / "workspace" / "achakrabort4")
    }
    at_user.run()
    assert len(at_user.exception) == 0
    user_radio_options = at_user.sidebar.radio[0].options
    # Rule 1: Regular User does not have Account Management in navigation
    assert not any("Account Management" in opt for opt in user_radio_options)


def test_chat_history_strict_isolation_between_users():
    """
    Verifies that conversations and messages created by User A are strictly invisible
    and inaccessible to User B, maintaining 100% privacy and zero cross-user leakage.
    """
    app_path = Path(__file__).parent.parent.parent / "streamlit_app.py"

    # 1. User A (achakrabort4) session
    at_a = AppTest.from_file(str(app_path.resolve()), default_timeout=30)
    at_a.session_state["authenticated_user"] = {
        "name": "Archiman Chakraborty",
        "username": "achakrabort4",
        "role": "User",
        "workspace_path": str(Path(__file__).parent.parent.parent / "workspace" / "achakrabort4")
    }
    at_a.run()
    assert len(at_a.exception) == 0
    conv_a_id = at_a.session_state.get("active_conv_id")
    assert conv_a_id is not None

    # 2. User B (crakha1) session
    at_b = AppTest.from_file(str(app_path.resolve()), default_timeout=30)
    at_b.session_state["authenticated_user"] = {
        "name": "Chandan Rakha",
        "username": "crakha1",
        "role": "User",
        "workspace_path": str(Path(__file__).parent.parent.parent / "workspace" / "crakha1")
    }
    at_b.run()
    assert len(at_b.exception) == 0
    conv_b_id = at_b.session_state.get("active_conv_id")
    assert conv_b_id is not None

    # 3. Verify that User A and User B have separate conversations
    assert conv_a_id != conv_b_id, "User A and User B must have distinct isolated conversations!"

    # 4. Verify User B cannot access User A's conversation via query param
    at_b_hack = AppTest.from_file(str(app_path.resolve()), default_timeout=30)
    at_b_hack.session_state["authenticated_user"] = {
        "name": "Chandan Rakha",
        "username": "crakha1",
        "role": "User",
        "workspace_path": str(Path(__file__).parent.parent.parent / "workspace" / "crakha1")
    }
    at_b_hack.query_params["conv"] = conv_a_id
    at_b_hack.run()
    assert len(at_b_hack.exception) == 0
    # The active conversation must NOT be User A's conversation
    assert at_b_hack.session_state.get("active_conv_id") != conv_a_id, "User B must NOT be able to open User A's conversation via ?conv= query parameter!"
