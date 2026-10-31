"""Every relative link in the Markdown documents points at a file that exists (and, when it names a heading, at a heading that exists)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = [ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / "agent" / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)\)")


def anchor(heading):
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path):
    return {anchor(m.group(1)) for m in re.finditer(r"(?m)^#{1,6}\s+(.*)$", path.read_text(encoding="utf-8"))}


def test_relative_links_in_the_documents_resolve():
    problems = []
    for doc in DOCS:
        for target in LINK.findall(doc.read_text(encoding="utf-8")):
            if re.match(r"^(https?:|mailto:|#)", target):
                if target.startswith("#") and target[1:] not in anchors(doc):
                    problems.append(f"{doc.relative_to(ROOT)}: no heading for {target}")
                continue
            path, _, fragment = target.partition("#")
            found = (doc.parent / path).resolve()
            if not found.exists():
                problems.append(f"{doc.relative_to(ROOT)}: {target} does not exist")
            elif fragment and found.suffix == ".md" and fragment not in anchors(found):
                problems.append(f"{doc.relative_to(ROOT)}: {target} has no such heading")
    assert not problems, "\n".join(problems)
