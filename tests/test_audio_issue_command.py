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
