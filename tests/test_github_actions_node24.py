"""Do not reintroduce Node.js 20 GitHub Actions to AUDIO workflows."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
MIN_MAJOR = {
    "actions/checkout": 5,
    "actions/setup-python": 6,
    "actions/upload-artifact": 6,
    "actions/download-artifact": 7,
    "actions/cache": 5,
    "actions/cache/restore": 5,
    "actions/cache/save": 5,
    "actions/configure-pages": 6,
    "actions/upload-pages-artifact": 5,
    "actions/deploy-pages": 5,
    "actions/setup-node": 6,
    "actions/github-script": 8,
}
USE = re.compile(r"^\s*(?:-\s*)?uses:\s*(actions/[\w/-]+)@v(\d+)\b", re.MULTILINE)


def test_github_actions_do_not_use_node20_legacy_majors():
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    assert len(workflows) >= 3
    obsolete = []
    for workflow in workflows:
        for name, version in USE.findall(workflow.read_text(encoding="utf-8")):
            minimum = MIN_MAJOR.get(name)
            if minimum is not None and int(version) < minimum:
                obsolete.append(f"{workflow.name}: {name}@v{version} (requires >=v{minimum})")
    assert not obsolete, "Outdated Node20 GitHub Actions:\n" + "\n".join(obsolete)
