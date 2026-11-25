"""Image generation transport has one module with backwards-compatible API."""
from src import cover_inference, metadata_gen


def test_cover_inference_is_shared_by_metadata():
    assert metadata_gen.download_ai_image is cover_inference.download_ai_image
    assert metadata_gen._hf_error_is_retryable is cover_inference._hf_error_is_retryable


def test_failure_classification_handles_transient_vs_permanent():
    class HttpError(RuntimeError):
        pass
    assert cover_inference._hf_error_is_retryable(HttpError("HTTP 429 Too Many Requests"))
    assert cover_inference._hf_error_is_retryable(HttpError("connection reset by peer"))
    assert not cover_inference._hf_error_is_retryable(HttpError("HTTP 403 Unauthorized"))
