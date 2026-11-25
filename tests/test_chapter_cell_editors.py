"""Inline title/number editing helpers are independent of catalog crawling."""
import inspect
from gui_components.chapter_cell_editors import edit_chapter_name, edit_normalized_number


def test_cell_editors_accept_explicit_refresh_and_render_callbacks():
    for editor in (edit_chapter_name, edit_normalized_number):
        params = inspect.signature(editor).parameters
        assert {"app", "chapter_tree", "event", "get_parts", "refresh", "render"}.issubset(params)


def test_cell_editor_module_has_no_global_network_or_github_dependency():
    import gui_components.chapter_cell_editors as module
    source = inspect.getsource(module)
    assert "requests" not in source
    assert "GitHubQueueStore" not in source
