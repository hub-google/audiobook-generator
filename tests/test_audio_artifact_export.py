import zipfile
from unittest.mock import patch
import pytest
from tools.export_run_artifacts import validate_run_ids, stream_run_artifacts


def test_artifact_ids_deduplicate_and_validate():
    assert validate_run_ids("20,21,20")==[20,21]
    with pytest.raises(ValueError):
        validate_run_ids("../../etc,5")


def test_artifacts_stream_directly_into_single_zip(tmp_path):
    class Response:
        def __init__(self,payload=None,blob=b""):
            self.payload=payload;self.blob=blob
        def raise_for_status(self):pass
        def json(self):return self.payload
        def iter_content(self,chunk_size):yield self.blob
        def __enter__(self):return self
        def __exit__(self,*args):pass
    class Session:
        headers={}
        def get(self,url,**kwargs):
            if "artifacts?" in url:
                from urllib.parse import urlsplit,parse_qs
                page=int(parse_qs(urlsplit(url).query)["page"][0])
                if page==1:
                    return Response({"artifacts":[{
                        "name":"作品分包","id":24,"expired":False,
                        "archive_download_url":"https://example.invalid/archive","size_in_bytes":8
                    }]})
                return Response({"artifacts":[]})
            return Response(blob=b"zip-data")
        def close(self):pass
    path=tmp_path/"combined.zip"
    with patch("tools.export_run_artifacts.requests.Session",return_value=Session()):
        count=stream_run_artifacts("owner/repo","token",[123],path)
    assert count==1
    with zipfile.ZipFile(path) as zf:
        assert zf.read("run-123/作品分包.zip")==b"zip-data"
