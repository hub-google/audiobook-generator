"""Upload services use explicit standard-library dependencies."""
import inspect

from src.youtube_upload import service_pool, orchestrator


def test_service_pool_has_no_module_reflection():
    assert "_get_symbol" not in inspect.getsource(service_pool)
    assert "sys.modules" not in inspect.getsource(service_pool)
    assert "path_exists_fn = os.path.exists" in inspect.getsource(service_pool)


def test_orchestrator_does_not_search_imported_uploader_for_dependencies():
    source = inspect.getsource(orchestrator)
    assert "_get_symbol" not in source
    assert "sys.modules" not in source
