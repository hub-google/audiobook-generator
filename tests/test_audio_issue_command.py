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


def test_replayed_edit_does_not_overwrite_newer_profile():
    from unittest.mock import Mock
    from src.cloud_queue import empty_queue, new_task
    from tools.audio_issue_command import apply_command
    q = empty_queue()
    task = new_task("https://69shuba.com/book/123/", "Demo", 1, 3)
    q["queue"] = [task]
    q["web_applied_issue_ids"] = [41]
    store = Mock()
    store.load.return_value = (q, None)
    profiles = Mock()
    command = {"op": "edit", "data": {"task_id": task["task_id"],
               "changes": {"book_title": "Stale title"}}}
    result, dispatch = apply_command(command, store, profiles, 41, "dummy", "owner/repo")
    assert "Already processed" in result and dispatch is False
    profiles.mutate.assert_not_called()
    store.mutate.assert_not_called()


@pytest.mark.parametrize("status,changes", [
    ("running", {"book_title": "Rejected title"}),
    ("queued", {"start_chapter": 3, "end_chapter": 1}),
    ("queued", {"excluded_chapters": [1, 2, 3]}),
])
def test_invalid_edit_does_not_mutate_book_profile(status, changes):
    from unittest.mock import Mock
    from src.cloud_queue import empty_queue, new_task
    from tools.audio_issue_command import apply_command
    q = empty_queue()
    task = new_task("https://69shuba.com/book/123/", "Demo", 1, 3)
    task["status"] = status
    q["queue"] = [task]
    store = Mock()
    store.load.return_value = (q, None)
    profiles = Mock()
    command = {"op": "edit", "data": {"task_id": task["task_id"], "changes": changes}}
    with pytest.raises(ValueError):
        apply_command(command, store, profiles, 42, "dummy", "owner/repo")
    profiles.mutate.assert_not_called()
    store.mutate.assert_not_called()


def test_save_review_rules_only_does_not_approve_or_dispatch():
    from unittest.mock import Mock, patch
    from src.cloud_queue import empty_queue, new_task
    from tools.audio_issue_command import apply_command
    state = empty_queue()
    task = new_task("https://69shuba.com/book/123/", "Sample", 1, 3)
    task["status"] = "waiting_review"
    task["scrape_run_id"] = 10
    task["cover_run_id"] = 11
    state["queue"] = [task]
    class Store:
        def load(self): return state, None
        def mutate(self, fn, message):
            nonlocal state
            state = fn(state)
            return state
    profiles=Mock()
    payload={"op":"review","data":{
        "task_id":task["task_id"],"remove_patterns":["廣告文字"],
        "review_only":True,
        "evidence":{"task_id":task["task_id"],"raw_fingerprint":"a"*64}
    }}
    verified={"raw_fingerprint":"a"*64,"remove_patterns":["廣告文字"],
              "keep_patterns":["正常文字"]}
    with patch("tools.audio_review_evidence.validate_review_evidence",return_value=verified):
        result, dispatch=apply_command(payload,Store(),profiles,598,"token","owner/repo")
    assert dispatch is False
    assert state["queue"][0]["status"]=="waiting_review"
    assert state["queue"][0].get("ad_review",{}).get("status")!="approved"
    assert 598 in state["web_applied_issue_ids"]
    profiles.mutate.assert_called_once()


def test_profile_sync_outbox_recovers_after_failed_github_write():
    from src.cloud_queue import empty_queue, new_task
    from src.book_profiles import empty_profiles
    from tools.audio_issue_command import apply_command
    state=empty_queue()
    task=new_task("https://69shuba.com/book/123/", "Old", 1, 3)
    state["queue"].append(task)
    class Store:
        def load(self): return state, None
        def mutate(self, fn, msg):
            nonlocal state
            state=fn(state)
            return state
    class Profiles:
        fail=True
        value=empty_profiles()
        writes=0
        def mutate(self, fn, msg):
            self.writes+=1
            if self.fail:
                raise RuntimeError("Simulated profile network outage")
            self.value=fn(self.value)
            return self.value
    profiles=Profiles()
    cmd={"op":"edit","data":{"task_id":task["task_id"],"changes":{"book_title":"New"}}}
    with pytest.raises(RuntimeError, match="network outage"):
        apply_command(cmd, Store(), profiles, 810, "dummy","owner/repo")
    assert state["queue"][0]["book_title"] == "New"
    assert state["web_profile_pending"]["810"][0]["book_title"] == "New"
    assert 810 in state["web_applied_issue_ids"]
    profiles.fail=False
    msg,dispatch=apply_command(cmd, Store(), profiles, 810, "dummy","owner/repo")
    assert "Already processed" in msg and dispatch is False
    assert state["web_profile_pending"] == {}
    assert len(profiles.value["books"]) == 1
    assert next(iter(profiles.value["books"].values()))["book_title"] == "New"


def test_newer_edit_repairs_previous_pending_before_apply():
    from src.cloud_queue import empty_queue, new_task
    from src.book_profiles import empty_profiles
    from tools.audio_issue_command import apply_command
    state=empty_queue()
    task=new_task("https://69shuba.com/book/123/", "Old", 1, 3)
    state["queue"].append(task)
    class Store:
        def load(self): return state, None
        def mutate(self, fn, msg):
            nonlocal state
            state=fn(state)
            return state
    class Profiles:
        def __init__(self): self.value=empty_profiles();self.fail=True
        def mutate(self, fn, msg):
            if self.fail: raise RuntimeError("Service unavailable")
            self.value=fn(self.value)
    ps=Profiles()
    def edit(issue,title):
        return apply_command({"op":"edit","data":{"task_id":task["task_id"],
                             "changes":{"book_title":title}}},Store(),ps,issue,"token","owner/repo")
    with pytest.raises(RuntimeError):
        edit(820,"One")
    with pytest.raises(RuntimeError):
        edit(821,"Two")
    assert state["queue"][0]["book_title"] == "One"  # Newer request is blocked safely.
    ps.fail=False
    edit(821,"Two")
    assert state["queue"][0]["book_title"] == "Two"
    assert next(iter(ps.value["books"].values()))["book_title"] == "Two"
    assert not state["web_profile_pending"]
