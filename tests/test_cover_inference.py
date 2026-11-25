"""Image generation transport has one module with backwards-compatible API."""
from unittest.mock import Mock

import pytest

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


def test_billing_error_is_reported_without_rest_fallback(monkeypatch):
    error = RuntimeError("Payment Required")
    error.response = Mock(status_code=402)
    client = Mock()
    client.text_to_image.side_effect = error
    rest = Mock()
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    monkeypatch.setattr("huggingface_hub.InferenceClient", Mock(return_value=client))
    monkeypatch.setattr(cover_inference.requests, "post", rest)

    with pytest.raises(RuntimeError, match="HTTP 402") as failure:
        cover_inference.download_ai_image("cover prompt")

    assert failure.value.__cause__ is error
    client.text_to_image.assert_called_once()
    rest.assert_not_called()
