"""Prevent a return to the known high-priority AUDIO architecture defects."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def code(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_new_services_are_independent_python_modules():
    for name in (
        "dispatch_reconciliation", "dispatch_execution", "dispatch_github_gateway",
        "dispatch_retry_policy", "cover_research", "cover_ai", "cover_inference",
        "chapter_selection_state",
    ):
        ast.parse(code(f"src/{name}.py"))


def test_metadata_and_dispatcher_do_not_duplicate_services():
    meta = code("src/metadata_gen.py")
    assert "def generate_gemini_cover_information(" not in meta
    assert "def collect_cover_research(" not in meta
    assert "def download_ai_image(" not in meta
    dispatcher = code("src/queue_dispatcher.py")
    assert "def reconcile(self, queue):" in dispatcher
    assert "def dispatch_next(self, queue," in dispatcher
    assert "def retry_state(" not in dispatcher


def test_studio_backfill_has_one_embeddable_panel_and_one_network_client():
    ui = code("tools/youtube_backfill_gui.py")
    backend = code("tools/youtube_backfill_backend.py")
    client = code("tools/youtube_studio_client.py")
    assert "class App(ttk.Frame):" in ui
    assert "class StudioPrivateClient:" not in ui
    assert "class StudioPrivateClient:" not in backend
    assert "class StudioPrivateClient:" in client
    assert "class MergeUploadGUI(ttk.Frame):" in code("合併上傳/gui.py")
    assert "subprocess.Popen" not in code("gui_components/tool_launchers.py")


def test_decommissioned_global_workflow_controls_stay_deleted():
    gui = code("gui_components/workflow.py")
    assert "def trigger_github_actions(" not in gui
    assert "def cancel_github_actions(" not in gui
    assert "def _poll_workflow_runs(" not in gui
    assert "def start_batch_download(" not in gui
    main = code("gui_app.py")
    assert "self.btn_download =" not in main
    assert "self.btn_cancel =" not in main
