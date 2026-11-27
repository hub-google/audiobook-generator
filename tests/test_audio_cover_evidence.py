"""Cover approval cannot claim a different book, revision or corrupted cover."""
import hashlib
import io
import json
import zipfile
from unittest.mock import patch
import pytest

from tools.audio_cover_evidence import validate_cover_review_evidence

TASK={"task_id":"book-20261127-1234abcd","cover_run_id":321}


class Response:
    def __init__(self, raw): self.raw=raw
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def raise_for_status(self): pass
    def iter_content(self, chunk_size): yield self.raw


def case(tamper=False,manifest_task=None):
    cover=b"fake jpeg, only used as an integrity test"
    prompt=b'{"prompt":"Original AI cover information"}'
    manifest={
        "schema_version":1,"artifact_role":"cover_preflight",
        "task_id":manifest_task or TASK["task_id"],
        "cover_sha256":hashlib.sha256(cover).hexdigest(),
        "prompt_sha256":hashlib.sha256(prompt).hexdigest(),
    }
    if tamper: cover+=b"unexpected change"
    payload=io.BytesIO()
    with zipfile.ZipFile(payload,"w") as z:
        for name,raw in (
            ("master_cover.jpg",cover),
            ("master_cover_prompt.json",prompt),
            ("cover-preflight-manifest.json",json.dumps(manifest).encode()),
        ):
            z.writestr("Workspace/book/Cover/"+name,raw)
    expected={"task_id":TASK["task_id"],
              "cover_sha256":manifest["cover_sha256"],
              "prompt_sha256":manifest["prompt_sha256"]}
    return payload.getvalue(),expected


def api(method,path,token,repo):
    assert path == "actions/runs/321/artifacts?per_page=100"
    return {"artifacts":[{"name":"cover-review-"+TASK["task_id"],
                           "expired":False,
                           "archive_download_url":"https://example.invalid/cover.zip"}]}


def test_cover_approval_uses_actual_github_artifact_and_manifest():
    raw,record=case()
    with patch("tools.audio_cover_evidence.requests.get",return_value=Response(raw)):
        result=validate_cover_review_evidence({"cover_evidence":record},
                             TASK,"secret","owner/repo",api)
    assert result["cover_verified_sha256"]==record["cover_sha256"]
    assert result["cover_prompt_verified_sha256"]==record["prompt_sha256"]


@pytest.mark.parametrize("altered,manifest_task",[(True,None),(False,"book-20261127-deadbeef")])
def test_cover_approval_rejects_changed_or_other_book_artifacts(altered,manifest_task):
    raw,record=case(tamper=altered,manifest_task=manifest_task)
    with patch("tools.audio_cover_evidence.requests.get",return_value=Response(raw)):
        with pytest.raises(ValueError):
            validate_cover_review_evidence({"cover_evidence":record},
                                           TASK,"secret","owner/repo",api)


def test_cover_approval_rejects_forged_fingerprint_without_download():
    _,record=case()
    record["cover_sha256"]="B"*64
    with pytest.raises(ValueError):
        validate_cover_review_evidence({"cover_evidence":record},
                                       TASK,"secret","owner/repo",api)
