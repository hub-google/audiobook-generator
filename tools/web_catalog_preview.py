"""Preview catalog via the same nine source adapters used in production."""
import argparse
import json
import os
import re
from pathlib import Path
from src.catalog_parser import parse_catalog, split_chapter_title, normalize_chapter_name_for_comparison, apply_chapter_title_overrides
from src.book_profiles import GitHubBookProfileStore, get_book_profile

def build_preview(url, profiles=None):
    """Build the SAME catalog/profile view as the desktop GUI, including edits."""
    result = parse_catalog(url)
    if not result.get("success") or not result.get("total_chapters"):
        raise RuntimeError("Catalog is incomplete")
    profile = {}
    if profiles is not None:
        _, profile = get_book_profile(profiles, result["catalog_url"], result["book_title"])
        apply_chapter_title_overrides(
            result,
            profile.get("chapter_title_overrides") or {},
            profile.get("chapter_normalized_number_overrides") or {},
        )
    preview = {key: result[key] for key in (
        "catalog_url", "book_title", "source_id", "total_chapters", "catalog_identity"
    )}
    preview.update(requested_url=url, request_id="", chapter_titles=result["chapter_titles"])
    parts = [split_chapter_title(title) for title in result["chapter_titles"]]
    preview["chapter_parts"] = [
        {"n": part["normalized_number"], "d": part["display_number"],
         "name": part["chapter_name"],
         "c": normalize_chapter_name_for_comparison(part["chapter_name"])}
        for part in parts
    ]
    preview["duplicate_chapter_count"] = result.get("duplicate_chapter_count", 0)
    preview["profile_defaults"] = {
        "chapter_title_overrides": result.get("chapter_title_overrides") or {},
        "chapter_normalized_number_overrides":
            result.get("chapter_normalized_number_overrides") or {},
        "duplicate_detection": profile.get("duplicate_detection") or {
            "use_normalized_number": True, "use_chapter_name": True,
            "use_number_and_name": False,
        },
    }
    return preview


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--request-id", required=True)
    a = p.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{8,40}", a.request_id):
        p.error("Invalid preview request ID")
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    profiles = GitHubBookProfileStore(repo, token).load()[0] if repo and token else None
    result = build_preview(a.url, profiles)
    result["request_id"] = a.request_id
    Path("catalog-preview.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Catalog parsed: {result['source_id']} {result['total_chapters']} chapters")


if __name__ == "__main__":
    main()
