from app.menu import resolve_menu_choice


def test_row_id_wins():
    assert resolve_menu_choice('home', interactive_id='tb_home_tools', text='anything') == 'tb_home_tools'


def test_visible_home_title_resolves():
    assert resolve_menu_choice('home', interactive_id=None, text='🤖 Agents') == 'tb_home_agents'


def test_poll_title_resolves_even_after_state_expiry():
    assert resolve_menu_choice(None, interactive_id=None, text='🔎 OCR Image') == 'tb_tool_ocr'


def test_typed_navigation_is_still_supported():
    assert resolve_menu_choice(None, interactive_id=None, text='tools') == 'tb_home_tools'
