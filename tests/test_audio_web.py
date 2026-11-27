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


def test_advanced_web_editor_and_review_inputs():
    html=(ROOT/"docs/site/index.html").read_text(encoding="utf-8")
    js=(ROOT/"docs/site/app.js").read_text(encoding="utf-8")
    for field in ("title-overrides", "number-overrides", "chapter-order", "edit-dialog", "edit-form", "review-dialog", "review-patterns"):
        assert f'id="{field}"' in html
    assert "function openEditor(id)" in js
    assert "async function saveEditor(event)" in js
    assert "async function saveReview(event)" in js
    assert 'ref:"master"' not in js


def test_workflow_allows_single_youtube_account_with_expandable_pool():
    workflow=(ROOT/".github/workflows/audiobook.yml").read_text(encoding="utf-8")
    assert 'YOUTUBE_EXPECTED_ACCOUNT_COUNT: "1"' in workflow
    assert "YOUTUBE_REFRESH_TOKEN_10:" in workflow


def test_web_progress_and_completion_controls():
    html=(ROOT/"docs/site/index.html").read_text(encoding="utf-8")
    js=(ROOT/"docs/site/app.js").read_text(encoding="utf-8")
    assert 'id="progress-dialog"' in html
    for label in ("cancelRunningTask","markCompleted","restoreCompleted","showProgress"):
        assert "function "+label+"(" in js or "async function "+label+"(" in js
    assert '"/actions/runs/"+encodeURIComponent(id)+"/cancel"' in js


def test_full_chapter_picker_keeps_complete_catalog():
    parser=(ROOT/"tools/web_catalog_preview.py").read_text(encoding="utf-8")
    html=(ROOT/"docs/site/index.html").read_text(encoding="utf-8")
    js=(ROOT/"docs/site/app.js").read_text(encoding="utf-8")
    assert 'chapter_titles=result["chapter_titles"]' in parser
    assert 'chapter_titles"][:60]' not in parser
    for id in ("chapter-dialog","chapter-list","chapter-search","chapter-all","chapter-none","chapter-invert","chapter-picker-save"):
        assert f'id="{id}"' in html
    assert "function pickerMatchingIndices()" in js


def test_publish_mode_matches_desktop_preset_and_taipei_strategy_a():
    html=(ROOT/"docs/site/index.html").read_text(encoding="utf-8")
    js=(ROOT/"docs/site/app.js").read_text(encoding="utf-8")
    assert 'id="publish-immediate"' in html
    assert 'id="publish-scheduled"' in html
    assert 'data-publish-day="2"' in html
    assert 'timeZone:"Asia/Taipei"' in js
    assert 'function renderPublish()' in js
    assert 'function setPublishPreset(day,hours)' in js


def test_web_chapter_duplicate_logic_matches_python_catalog_parser():
    parser=(ROOT/"tools/web_catalog_preview.py").read_text(encoding="utf-8")
    js=(ROOT/"docs/site/app.js").read_text(encoding="utf-8")
    html=(ROOT/"docs/site/index.html").read_text(encoding="utf-8")
    assert "split_chapter_title" in parser
    assert "normalize_chapter_name_for_comparison" in parser
    assert "function analyzePickerDuplicates()" in js
    assert "function sortPickerOrder(mode)" in js
    assert "function movePickerChapter(index,delta)" in js
    for id in ("duplicate-number","duplicate-name","duplicate-pair","duplicate-only","chapter-exclude-duplicates","chapter-sort-number","chapter-sort-original"):
        assert f'id="{id}"' in html


def test_public_read_only_progress_does_not_require_browser_token():
    js=(ROOT/"docs/site/app.js").read_text(encoding="utf-8")
    html=(ROOT/"docs/site/index.html").read_text(encoding="utf-8")
    assert 'div.append(makeBtn("進度",showProgress,task.task_id,false,false));' in js
    assert 'function makeBtn(label,action,id,disabled=false,authRequired=true)' in js
    assert 'async function renderRunJobDetails(task)' in js
    assert 'id="progress-jobs"' in html


def test_69shuba_ranking_has_real_data_export_and_read_only_browser_view():
    exporter=(ROOT/"tools/export_69shuba_ranking.py").read_text(encoding="utf-8")
    workflow=(ROOT/".github/workflows/audio-web-pages.yml").read_text(encoding="utf-8")
    html=(ROOT/"docs/site/index.html").read_text(encoding="utf-8")
    js=(ROOT/"docs/site/app.js").read_text(encoding="utf-8")
    assert "fetch_69shuba_full_novels()" in exporter
    assert "python -m tools.export_69shuba_ranking" in workflow
    assert 'id="rank-search"' in html
    assert 'id="rank-body"' in html
    assert "async function loadRanking()" in js
    assert "async function copyRankingUrls()" in js
    assert "async function addRankingToQueue()" not in js


def test_github_pages_only_uses_issue_bridge_no_browser_pat():
    html=(ROOT/"docs/site/index.html").read_text(encoding="utf-8")
    js=(ROOT/"docs/site/app.js").read_text(encoding="utf-8")
    bridge=(ROOT/"docs/site/bridge.js").read_text(encoding="utf-8")
    assert 'id="token"' not in html
    assert 'src="./bridge.js"' in html
    assert 'headers.Authorization="Bearer "' not in js
    assert 'AUDIO-COMMAND-V1' in bridge
    assert "hub-google/audiobook-generator/issues/new" in bridge
    assert 'window.addRankingToQueue=async function()' in bridge
