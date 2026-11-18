"""守门：git archive HEAD 导出的公开快照不含内部记录和个人名字，且仍自洽。"""

import io
import re
import subprocess
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXCLUDED = ("docs/decisions.md", "docs/execution-report.md", "docs/proposals")
KEPT = ("docs/plan.md", "docs/process/execute-review-improve.md")
NAME = "le" + "to"  # 运行时拼接，避免本文件自己命中扫描
NAME_RE = re.compile(rf"(?<![A-Za-z]){NAME}(?![A-Za-z])", re.IGNORECASE)


def _archive() -> tarfile.TarFile:
    data = subprocess.run(
        ["git", "archive", "HEAD"], cwd=REPO, check=True, capture_output=True
    ).stdout
    return tarfile.open(fileobj=io.BytesIO(data))


def _names() -> list[str]:
    return [n.rstrip("/") for n in _archive().getnames()]


def test_archive_excludes_internal_records() -> None:
    names = _names()
    offenders = [n for n in names for ex in EXCLUDED if n == ex or n.startswith(ex + "/")]
    assert not offenders, f"公开快照含内部记录: {offenders}"


def test_archive_keeps_plan_and_process() -> None:
    names = set(_names())
    for path in KEPT:
        assert path in names


def test_archive_text_files_do_not_mention_name() -> None:
    offenders = []
    with _archive() as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            raw = tar.extractfile(member).read()  # type: ignore[union-attr]
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if NAME_RE.search(text):
                offenders.append(member.name)
    assert not offenders, f"公开快照出现不该有的名字: {offenders}"
