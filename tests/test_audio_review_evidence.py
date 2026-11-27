import io
import json
import zipfile
from unittest.mock import patch
import pytest
from tools.audio_review_evidence import validate_review_evidence


def test_authoritative_raw_fingerprint_and_keep_patterns():
    task={"task_id":"book-20261127-1234abcd","scrape_run_id":123,"cover_run_id":124}
    report={"artifact_role":"ad_review_candidates","task_id":task["task_id"],
            "raw_fingerprint":"a"*64,
            "candidates":[{"text":"廣告詞"},{"text":"正常詞"}]}
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,"w") as z:
        z.writestr("ad-candidates-full-book.json",json.dumps(report,ensure_ascii=False))
    class Response:
        def __init__(self,data):self.data=data
        def raise_for_status(self):pass
        def iter_content(self,chunk_size):yield self.data
        def __enter__(self):return self
        def __exit__(self,*args):pass
    artifact={"artifacts":[{"name":"scrape-review-"+task["task_id"],
                            "expired":False,"archive_download_url":"https://example.invalid/archive"}]}
    def api(method,path,token,repo):
        return artifact
    with patch("tools.audio_review_evidence.requests.get",return_value=Response(buffer.getvalue())):
        evidence=validate_review_evidence(
            {"evidence":{"task_id":task["task_id"],"raw_fingerprint":"a"*64},
             "remove_patterns":["廣告詞"]},task,"fake","owner/repo",api)
    assert evidence["keep_patterns"]==["正常詞"]
    assert evidence["scrape_run_id"]==123
    with patch("tools.audio_review_evidence.requests.get",return_value=Response(buffer.getvalue())):
        with pytest.raises(ValueError):
            validate_review_evidence(
                {"evidence":{"task_id":task["task_id"],"raw_fingerprint":"b"*64}},
                task,"fake","owner/repo",api)
