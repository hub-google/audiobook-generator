"""Unit coverage for safe, ordered private ZIP transport."""
from pathlib import Path
import io
import json
import zipfile

import pytest

from tools.stage_audio_export import CHUNK_SIZE, chunk_names, stage_export


def test_default_chunks_fit_edge_upload_headroom():
    assert CHUNK_SIZE == 4_000_000
    chunks=list(chunk_names("audio-export-11.zip",10_000_001))
    assert [length for _,_,length in chunks] == [4_000_000,4_000_000,2_000_001]


def test_split_chunks_have_exact_offsets_and_size():
    chunks=list(chunk_names("audio-export-5.zip",51_000_001,chunk_size=25_000_000))
    assert chunks==[
        ("audio-export-5.zip.part00001-of-00003",0,25_000_000),
        ("audio-export-5.zip.part00002-of-00003",25_000_000,25_000_000),
        ("audio-export-5.zip.part00003-of-00003",50_000_000,1_000_001),
    ]
    assert sum(c for _,_,c in chunks)==51_000_001


def test_small_zip_keeps_single_filename():
    assert list(chunk_names("audio-export-5.zip",101))==[
        ("audio-export-5.zip",0,101)
    ]


def test_reject_empty_archive():
    with pytest.raises(ValueError):
        list(chunk_names("audio-export.zip",0))


def test_stage_chunks_are_complete_and_ordered(monkeypatch,tmp_path):
    data=bytes((i*91)%256 for i in range(2000))
    archive=tmp_path/"audio-export.zip"
    with zipfile.ZipFile(archive,"w",compression=zipfile.ZIP_STORED) as out:
        out.writestr("contents.txt",data)
    calls=[]
    class MockResponse:
        def __init__(self,n): self.n=n
        def __enter__(self): return self
        def __exit__(self,*args): return False
        def read(self): return json.dumps({"ok":True,"asset":{"id":self.n}}).encode()
    def fake_urlopen(request,timeout):
        calls.append((request.headers["X-audio-filename"],request.data))
        return MockResponse(len(calls))
    monkeypatch.setattr("tools.stage_audio_export.urlopen",fake_urlopen)
    assert stage_export(archive,42,"mock-gh-oidc",chunk_size=400)==len(calls)
    assert len(calls)>3
    assert b"".join(blob for _,blob in calls)==archive.read_bytes()
    assert all(".part" in name for name,_ in calls)
    assert calls[0][0].endswith("-of-%05d"%len(calls))
