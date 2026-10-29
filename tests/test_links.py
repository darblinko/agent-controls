"""Every relative link and image in the Markdown and HTML files resolves to a file."""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".pytest_cache", ".ruff_cache", ".venv", "node_modules"}
LINK_PATTERNS = [
    re.compile(r"\[[^\]]*\]\(([^)\s]+)\)"),
    re.compile(r"""(?:src|href|srcset)=["']([^"']+)["']"""),
]
EXTERNAL = ("http://", "https://", "mailto:", "#", "data:")


def documents() -> list[Path]:
    found = []
    for path in sorted(REPO.rglob("*")):
        if path.suffix in {".md", ".html"} and not SKIP_DIRS & set(path.parts):
            found.append(path)
    return found


def relative_targets(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    targets = []
    for pattern in LINK_PATTERNS:
        for match in pattern.finditer(text):
            target = match.group(1).split()[0]
            if not target.startswith(EXTERNAL) and "$" not in target:
                targets.append(target.split("#", 1)[0])
    return [target for target in targets if target]


@pytest.mark.parametrize("path", documents(), ids=lambda p: str(p.relative_to(REPO)))
def test_relative_links_resolve(path):
    missing = [t for t in relative_targets(path) if not (path.parent / t).exists()]
    assert not missing, f"{path.relative_to(REPO)}: {missing}"
