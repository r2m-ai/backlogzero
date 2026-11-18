"""守门：受管文件里不得出现 /Users/<用户名>/ 形式的本机绝对路径。"""

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
USERS_DIR = "/Us" + "ers/"  # 运行时拼接，避免本文件自己命中扫描
HOME_PATH = re.compile(USERS_DIR + r"([^/\s\"'`)]+)/")
PLACEHOLDER_USERS = frozenset({"<用户名>", "<user>", "<name>", "you", "me", "user", "username"})


def _is_placeholder(name: str) -> bool:
    return len(name) == 1 or name in PLACEHOLDER_USERS


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, check=True, capture_output=True
    ).stdout.decode()
    return [p for p in out.split("\0") if p]


def find_home_paths(text: str) -> list[str]:
    return [m.group(0) for m in HOME_PATH.finditer(text) if not _is_placeholder(m.group(1))]


def test_detects_real_username_path() -> None:
    assert find_home_paths(f"see {USERS_DIR}alice/work/x") == [f"{USERS_DIR}alice/"]


def test_allows_single_char_and_named_placeholders() -> None:
    assert find_home_paths(f"{USERS_DIR}x/.cache {USERS_DIR}m/.cache {USERS_DIR}<用户名>/a") == []


def test_tracked_files_have_no_home_paths() -> None:
    offenders = []
    for rel in _tracked_files():
        path = REPO / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        offenders += [f"{rel}: {hit}" for hit in find_home_paths(text)]
    assert not offenders, "受管文件含本机绝对路径：\n" + "\n".join(offenders)
