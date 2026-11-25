"""Guard the architectural simplifications introduced by the refactor."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "gui_components"


def test_dialogs_do_not_import_runtime_using_star():
    for path in COMPONENTS.glob("*.py"):
        assert "from .runtime import *" not in path.read_text(encoding="utf-8"), path


def test_runtime_does_not_duplicate_src_module_identity():
    code = (COMPONENTS / "runtime.py").read_text(encoding="utf-8")
    assert "sys.path.append" not in code
    assert "from src.catalog_parser import" in code
    assert "from catalog_parser import" not in code
    assert "GitHubQueueStore = None" not in code
    app = (ROOT / "gui_app.py").read_text(encoding="utf-8")
    assert "sys.path.append" not in app
    assert "from src.catalog_parser import" in app
    assert "GitHubQueueStore = None" not in app


def test_durable_run_settlement_is_owned_by_dispatcher():
    code = (COMPONENTS / "queue_monitor.py").read_text(encoding="utf-8")
    body = code.split("    def sync_cloud_queue(self):", 1)[1].split(
        "    @staticmethod\n    def _apply_preflight_observations", 1
    )[0]
    assert "store.mutate(" not in body
    assert "mark_task_completed(" not in body
    assert "mark_task_waiting_retry(" not in body
    assert "self._dispatch_queue_workflow()" in body


def test_only_production_source_registry_is_used_by_live_checks():
    from src.sources.registry import SOURCES
    from tools.validate_sources_live import SAMPLES
    assert set(SOURCES) == set(SAMPLES)
    assert len(SOURCES) == 9


def test_obsolete_cover_and_reader_debug_workflows_removed():
    code = (ROOT / "src" / "metadata_gen.py").read_text(encoding="utf-8")
    assert "_create_youtube_cover_legacy" not in code
    assert "_create_youtube_cover_redesign" not in code
    workflows = list((ROOT / ".github" / "workflows").glob("*.yml"))
    assert len(workflows) == 13
    names = {path.name for path in workflows}
    assert "source-live-validation.yml" in names
    assert "queue-dispatcher.yml" in names
    assert "diagnose-remaining-sources.yml" not in names


def test_ci_python_steps_are_not_scattered_in_yaml():
    from src.ci.audiobook_steps import STEP_NAMES
    code = (ROOT / ".github" / "workflows" / "audiobook.yml").read_text(encoding="utf-8")
    assert len(STEP_NAMES) == 9
    assert "python - <<'PY'" not in code
    for name in STEP_NAMES:
        assert f"python -m src.ci.audiobook_steps {name}" in code
