"""Generate a public, read-only 69shuba finished-novel ranking for Pages."""
import json
from datetime import datetime, timezone
from pathlib import Path
from src.catalog_parser import fetch_69shuba_full_novels

def main():
    novels=fetch_69shuba_full_novels()
    if not novels:
        raise RuntimeError("69shuba ranking returned no books; do not publish fabricated data")
    allowed=("rank","title","author","category","status","latest_chapter","catalog_url")
    result={"updated_at":datetime.now(timezone.utc).isoformat(),
            "source":"https://69shuba.com/novels/full",
            "novels":[{k:item.get(k) for k in allowed} for item in novels]}
    target=Path("docs/site/full-novels.json")
    target.write_text(json.dumps(result,ensure_ascii=False,separators=(",",":")),encoding="utf-8")
    print(f"Prepared {len(novels)} ranking entries for GitHub Pages")

if __name__=="__main__":
    main()
