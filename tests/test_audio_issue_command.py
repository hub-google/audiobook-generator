"""GitHub-only browser command bridge security and queue behavior."""
import base64
import gzip
import json
from unittest.mock import patch
import pytest
from tools.audio_issue_command import decode_command, authorized, valid_ids, build_book

def packed(data):
    raw=gzip.compress(json.dumps(data,ensure_ascii=False).encode())
    return "<!-- AUDIO-COMMAND-V1:"+base64.urlsafe_b64encode(raw).decode().rstrip("=")+":AUDIO-COMMAND-V1 -->"

def test_decode_only_allowlisted_commands():
    assert decode_command(packed({"version":1,"op":"dispatch","data":{}}))["op"]=="dispatch"
    with pytest.raises(ValueError):
        decode_command(packed({"version":1,"op":"shell","data":{"command":"print('hi')"}}))
    with pytest.raises(ValueError):
        decode_command("fake")
    with pytest.raises(ValueError):
        decode_command(packed({"version":2,"op":"dispatch","data":{}}))

def test_rejects_non_collaborator():
    with patch("tools.audio_issue_command.api",return_value={"permission":"read"}):
        assert not authorized("visitor", "mock","owner/repo")
    with patch("tools.audio_issue_command.api",return_value={"permission":"admin"}):
        assert authorized("owner","mock","owner/repo")

def test_task_ids_must_be_valid():
    with pytest.raises(ValueError):
        valid_ids(["untrusted"])
    with pytest.raises(ValueError):
        valid_ids(["book-20261127-1234abcd"]*2)

def test_new_book_uses_full_catalog_not_first_ten_chapters():
    catalog={"success":True,"catalog_url":"https://69shuba.com/book/111/",
             "book_title":"測試", "total_chapters":2000,"catalog_identity":"hello",
             "chapter_titles":[str(i) for i in range(2000)],
             "duplicate_chapter_count":3}
    with patch("tools.audio_issue_command.parse_catalog",return_value=catalog):
        entry={"catalog_url":"https://69shuba.com/book/111/"}
        with patch("tools.audio_issue_command.valid_url",return_value=entry["catalog_url"]):
            book=build_book(entry,105)
    assert book["end_chapter"]==2000
    assert book["request_issue"]==105
    assert len(book["chapter_order"])==2000


def test_manual_cover_accepts_only_github_native_image_uploads():
    from tools.audio_issue_command import cover_attachment_url
    valid="![cover](https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc)"
    assert cover_attachment_url(valid).startswith("https://github.com/user-attachments/assets/")
    with pytest.raises(ValueError):
        cover_attachment_url("https://untrusted.invalid/my.jpg")
    with pytest.raises(ValueError):
        cover_attachment_url(valid+" "+valid)


def test_queue_mutations_are_idempotent_by_issue_number():
    from src.cloud_queue import empty_queue,new_task
    from tools.audio_issue_command import apply_command
    task=new_task("https://69shuba.com/book/100/","Sample",1,3)
    class Store:
        def __init__(self):
            self.queue=empty_queue()
            self.queue["queue"]=[task]
        def load(self):
            return self.queue,None
        def mutate(self,fn,msg):
            self.queue=fn(self.queue)
    store=Store()
    request={"op":"status","data":{"task_id":task["task_id"],"status":"paused"}}
    first,dispatch=apply_command(request,store,None,239,"none","owner/repo")
    assert dispatch is True
    revision=store.queue["revision"]
    assert 239 in store.queue["web_applied_issue_ids"]
    second,dispatch=apply_command(request,store,None,239,"none","owner/repo")
    assert dispatch is False
    assert store.queue["revision"]==revision
    assert "Already processed" in second


def test_duplicate_detection_cannot_disable_all_conditions():
    from tools.audio_issue_command import checked_detection
    with pytest.raises(ValueError):
        checked_detection({"use_normalized_number":False,"use_chapter_name":False,
                           "use_number_and_name":False})
    assert checked_detection({"use_chapter_name":True})["use_chapter_name"] is True


def test_batch_export_uses_only_authorized_task_runs():
    from src.cloud_queue import empty_queue,new_task
    from tools.audio_issue_command import apply_command
    q=empty_queue()
    t=new_task("https://69shuba.com/book/123/","Demo",1,2)
    t["run_id"]=101;t["scrape_run_id"]=102;t["cover_run_id"]=103
    q["queue"].append(t)
    class Store:
        def load(self):return q,None
    called=[]
    def api_mock(method,path,token,repo,**kwargs):
        called.append((method,path,kwargs));return {}
    with patch("tools.audio_issue_command.api",side_effect=api_mock):
        result,should_dispatch=apply_command({"op":"export_artifacts","data":{"task_ids":[t["task_id"]]}},Store(),None,365,"dummy","hub-google/audiobook-generator")
    assert "3 Runs" in result and should_dispatch is False
    assert called[0][1]=="actions/workflows/audio-export-artifacts.yml/dispatches"
    assert called[0][2]["json"]["inputs"]["source_run_ids"]=="101,102,103"
