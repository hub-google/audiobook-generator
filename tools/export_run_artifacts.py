"""Download original GitHub Actions Artifact ZIPs into a single streaming ZIP."""
import os
import re
import zipfile
from pathlib import Path
import requests

API = "https://api.github.com"
IDS = re.compile(r"^\d+(?:,\d+)*$")
SAFE_NAME = re.compile(r"[^\w.\-\u3400-\u9fff]", re.UNICODE)


def validate_run_ids(raw):
    raw = (raw or "").strip()
    if not IDS.fullmatch(raw):
        raise ValueError("Expected comma-separated GitHub Run IDs")
    return list(dict.fromkeys(int(v) for v in raw.split(",")))


def unique_name(run_id, item, used):
    name = SAFE_NAME.sub("_", str(item.get("name") or "unnamed"))
    file = f"run-{run_id}/{name}.zip"
    if file in used:
        file = f"run-{run_id}/{name}-{item['id']}.zip"
    used.add(file)
    return file


def stream_run_artifacts(repo, token, run_ids, destination):
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    count = 0
    names = set()
    try:
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as output:
            for run_id in run_ids:
                page = 1
                while True:
                    result = session.get(
                        f"{API}/repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100&page={page}",
                        timeout=(15,90),
                    )
                    result.raise_for_status()
                    batch = result.json().get("artifacts") or []
                    if not batch:
                        break
                    for item in batch:
                        if item.get("expired"):
                            continue
                        path = unique_name(run_id, item, names)
                        with session.get(item["archive_download_url"], stream=True, timeout=(15,240)) as response:
                            response.raise_for_status()
                            with output.open(path, "w", force_zip64=True) as writer:
                                for chunk in response.iter_content(chunk_size=1024*1024):
                                    if chunk:
                                        writer.write(chunk)
                        count += 1
                        print(f"[AUDIO_EXPORT] {count} {path}", flush=True)
                    page += 1
        if not count:
            raise ValueError("No nonexpired artifacts found in the selected Runs")
        return count
    except Exception:
        Path(destination).unlink(missing_ok=True)
        raise
    finally:
        session.close()


def main():
    request_id = os.environ["EXPORT_REQUEST_ID"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,90}", request_id):
        raise ValueError("Invalid request ID")
    count = stream_run_artifacts(
        os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"],
        validate_run_ids(os.environ["SOURCE_RUN_IDS"]), "audio-export.zip",
    )
    print(f"[AUDIO_EXPORT] {count} archives ready for one download", flush=True)


if __name__ == "__main__":
    main()
