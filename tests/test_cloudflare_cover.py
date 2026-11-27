import base64
import io
from unittest.mock import Mock, patch

import pytest
from PIL import Image

from src.cloudflare_cover import MODEL, generate_cloudflare_cover, prepare_prompt


def configure(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_WORKERS_FREE_CONFIRMED", "true")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a" * 32)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-token")


def test_free_confirmation_required(monkeypatch):
    configure(monkeypatch)
    monkeypatch.delenv("CLOUDFLARE_WORKERS_FREE_CONFIRMED")
    with patch("src.cloudflare_cover.requests.post") as request:
        with pytest.raises(RuntimeError, match="Free"):
            generate_cloudflare_cover("test")
        request.assert_not_called()


def test_image_response_and_output_dimensions(monkeypatch):
    configure(monkeypatch)
    buffer = io.BytesIO()
    Image.new("RGB", (1024, 1024), "blue").save(buffer, "JPEG")
    response = Mock(status_code=200)
    response.json.return_value = {"success": True, "result": {"image": base64.b64encode(buffer.getvalue()).decode()}}
    with patch("src.cloudflare_cover.requests.post", return_value=response) as request:
        image = generate_cloudflare_cover("cinematic fantasy scene")
    assert image.size == (1280, 720)
    assert image.mode == "RGB"
    assert request.call_count == 1
    assert request.call_args.args[0].endswith(MODEL)
    assert request.call_args.kwargs["json"]["steps"] == 4


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_api_failure_never_retries_or_falls_back(monkeypatch, status):
    configure(monkeypatch)
    with patch("src.cloudflare_cover.requests.post", return_value=Mock(status_code=status)) as request:
        with pytest.raises(RuntimeError, match=str(status)):
            generate_cloudflare_cover("test")
        request.assert_called_once()


def test_invalid_image(monkeypatch):
    configure(monkeypatch)
    response = Mock(status_code=200)
    response.json.return_value = {"success": True, "result": {"image": "invalid"}}
    with patch("src.cloudflare_cover.requests.post", return_value=response):
        with pytest.raises(RuntimeError, match="圖片資料無效"):
            generate_cloudflare_cover("test")


def test_prompt_length_and_crop_instruction():
    prompt = prepare_prompt("fantasy hero " * 400)
    assert len(prompt) <= 2048
    assert "central widescreen crop" in prompt
    assert "No text" in prompt


def test_router_never_uses_old_providers_when_cloudflare_enabled(monkeypatch):
    from src import metadata_gen
    configure(monkeypatch)
    monkeypatch.setenv("COVER_IMAGE_PROVIDER", "auto")
    with patch("src.cloudflare_cover.generate_cloudflare_cover", side_effect=RuntimeError("429")) as cf, \
         patch("src.metadata_gen._hf_download_ai_image") as hf, \
         patch("src.metadata_gen._gemini_generate_cover") as gemini:
        with pytest.raises(RuntimeError, match="429"):
            metadata_gen.download_ai_image("test")
        cf.assert_called_once()
        hf.assert_not_called()
        gemini.assert_not_called()
