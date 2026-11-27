from pathlib import Path
from unittest.mock import Mock, patch
from PIL import Image

from src.metadata_gen import download_ai_image

ROOT=Path(__file__).resolve().parents[1]


def test_pages_assets_and_nine_source_registry():
    html=(ROOT/"docs/site/index.html").read_text(encoding="utf-8")
    js=(ROOT/"docs/site/app.js").read_text(encoding="utf-8")
    assert 'src="./app.js"' in html
    assert "sessionStorage" not in js and "localStorage" not in js
    from src.sources.registry import SOURCES
    names={"hjwzw","shuba69","czbooks","sixk","xszj","metanovel","twkan","moon17","book8"}
    assert set(SOURCES)==names
    for name in names:
        assert f'id:"{name}"' in js


@patch.dict("os.environ",{"COVER_IMAGE_PROVIDER":"auto","HF_TOKEN":"","GEMINI_API_KEY":"demo"},clear=False)
@patch("src.metadata_gen._gemini_generate_cover",return_value=Image.new("RGB",(1280,720)))
def test_missing_hf_uses_gemini(generate):
    assert download_ai_image("cover",max_attempts=1).size==(1280,720)
    generate.assert_called_once()


@patch.dict("os.environ",{"COVER_IMAGE_PROVIDER":"auto","HF_TOKEN":"hf_demo","GEMINI_API_KEY":"demo"},clear=False)
@patch("src.metadata_gen._hf_download_ai_image",side_effect=RuntimeError("HTTP 402 credit exhausted"))
@patch("src.metadata_gen._gemini_generate_cover",return_value=Image.new("RGB",(1280,720)))
def test_hf_credits_exhausted_uses_gemini(gemini,hf):
    assert download_ai_image("cover",max_attempts=1).size==(1280,720)
    hf.assert_called_once()
    gemini.assert_called_once()


@patch.dict("os.environ",{"COVER_IMAGE_PROVIDER":"gemini","GEMINI_API_KEY":"demo","GEMINI_IMAGE_MODEL":"gemini-2.5-flash-image"},clear=False)
@patch("src.metadata_gen.requests.post")
def test_gemini_image_api_mock(post):
    import base64,io
    from src.metadata_gen import _gemini_generate_cover
    buf=io.BytesIO()
    Image.new("RGB",(1024,1024)).save(buf,format="PNG")
    resp=Mock(status_code=200)
    resp.json.return_value={"candidates":[{"content":{"parts":[{"inlineData":{"data":base64.b64encode(buf.getvalue()).decode("ascii")}}]}}]}
    post.return_value=resp
    image=_gemini_generate_cover("cover",1280,720,max_attempts=1)
    assert image.size==(1280,720)
    assert "gemini-2.5-flash-image" in post.call_args.args[0]
    assert post.call_args.kwargs["json"]["generationConfig"]["imageConfig"]["aspectRatio"]=="16:9"


def test_cover_preflight_stays_independent_and_receives_gemini_secret():
    workflow=(ROOT/".github/workflows/cover-preflight.yml").read_text(encoding="utf-8")
    assert "GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}" in workflow
    assert "catalog_parser.py" not in workflow
    assert "crawler.py" not in workflow
