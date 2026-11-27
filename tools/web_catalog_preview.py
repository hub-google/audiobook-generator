"""Preview catalog via the same nine source adapters used in production."""
import argparse
import json
import re
from pathlib import Path
from src.catalog_parser import parse_catalog

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--url",required=True)
    p.add_argument("--request-id",required=True)
    a=p.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{8,40}",a.request_id):
        p.error("Invalid preview request ID")
    result=parse_catalog(a.url)
    if not result.get("success") or not result.get("total_chapters"):
        raise RuntimeError("Catalog is incomplete")
    preview={key:result[key] for key in ("catalog_url","book_title","source_id","total_chapters","catalog_identity")}
    preview.update(requested_url=a.url,request_id=a.request_id,chapter_titles=result["chapter_titles"][:60])
    Path("catalog-preview.json").write_text(json.dumps(preview,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"Catalog parsed: {preview['source_id']} {preview['total_chapters']} chapters")

if __name__=="__main__":
    main()
