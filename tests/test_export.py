import os
import subprocess
from pathlib import Path

import pytest

from agentops import cli

FAKE_NAME = "Alice"


def git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout


def make_repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def commit_all(repo: Path) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "change")


@pytest.fixture
def repos(tmp_path, agentops_home, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("LOGNAME", "bob_runner")
    monkeypatch.setenv("USER", "bob_runner")
    source = make_repo(
        tmp_path / "src",
        {
            "keep.txt": "same\n",
            "mod.txt": "new\n",
            "gone.txt": "x\n",
            "sub/deep.txt": "d\n",
            ".gitignore": ".venv/\n",
        },
    )
    target = make_repo(
        tmp_path / "pub",
        {
            "keep.txt": "same\n",
            "mod.txt": "old\n",
            "old.txt": "o\n",
            "gone.txt": "x\n",
            "sub/deep.txt": "d\n",
            "sub/old2.txt": "o\n",
            ".gitignore": ".venv/\n",
        },
    )
    return source, target


def baseline(source: Path, target: Path) -> None:
    assert run(source, target) == 0
    commit_all(target)


def run(source: Path, target: Path) -> int:
    return cli.main(["export", "--from", str(source), "--to", str(target)])


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(root).parts[:1]
    }


def test_sync_adds_modifies_deletes(repos, capsys):
    source, target = repos
    (source / "added.txt").write_text("a\n")
    git(source, "rm", "-q", "gone.txt")
    commit_all(source)

    assert run(source, target) == 0

    out = capsys.readouterr().out
    assert (target / "added.txt").read_text() == "a\n"
    assert (target / "mod.txt").read_text() == "new\n"
    assert not (target / "old.txt").exists()
    assert not (target / "sub" / "old2.txt").exists()
    assert "新增: 1" in out and "修改: 1" in out and "删除: 3" in out
    assert git(source, "rev-parse", "--short", "HEAD").strip() in out
    assert "git -C" in out and "push" in out
    assert git(target, "log", "--oneline").count("\n") == 1  # 没有新提交


def test_executable_bit_change_is_a_modification(repos, capsys):
    source, target = repos
    baseline(source, target)
    (source / "keep.txt").chmod(0o755)
    commit_all(source)

    assert run(source, target) == 0

    assert "修改: 1" in capsys.readouterr().out
    assert os.access(target / "keep.txt", os.X_OK)


def test_export_ignore_files_do_not_appear(repos):
    source, target = repos
    (source / ".gitattributes").write_text("secret.md export-ignore\n")
    (source / "secret.md").write_text("internal\n")
    commit_all(source)

    assert run(source, target) == 0

    assert not (target / "secret.md").exists()


def test_no_changes_reports_up_to_date(repos, capsys):
    source, target = repos
    baseline(source, target)
    capsys.readouterr()

    assert run(source, target) == 0

    assert "公开版已是最新" in capsys.readouterr().out


def test_dirty_target_is_rejected(repos, capsys):
    source, target = repos
    (target / "stray.txt").write_text("x\n")
    before = snapshot(target)

    assert run(source, target) == 2

    assert snapshot(target) == before
    assert "不干净" in capsys.readouterr().err


def test_dirty_source_is_rejected(repos):
    source, target = repos
    (source / "mod.txt").write_text("uncommitted\n")
    before = snapshot(target)

    assert run(source, target) == 2

    assert snapshot(target) == before


def test_target_not_a_git_repo_is_rejected(repos, tmp_path):
    source, _ = repos
    plain = tmp_path / "plain"
    plain.mkdir()

    assert run(source, plain) == 2


def test_overlapping_paths_are_rejected(repos, tmp_path):
    source, target = repos
    inner = make_repo(source / "inner", {"f.txt": "1\n"})
    (source / ".gitignore").write_text("inner/\n")
    commit_all(source)
    before = snapshot(source)

    assert run(source, source) == 2
    assert run(source, inner) == 2
    assert run(inner, source) == 2
    assert snapshot(source) == before


@pytest.mark.parametrize("kind", ["redact", "home", "user"])
def test_scan_hit_rejects_and_leaves_target_untouched(repos, capsys, tmp_path, monkeypatch, kind):
    source, target = repos
    if kind == "redact":
        (agentops_cfg := Path(os.environ["AGENTOPS_HOME"]) / "config.toml").write_text(
            f'[review]\nredact = ["{FAKE_NAME}"]\n'
        )
        assert agentops_cfg.exists()
        leak = f"author {FAKE_NAME}\n"
    elif kind == "home":
        leak = f"path {tmp_path / 'fakehome'}/x\n"
    else:
        leak = "who bob_runner\n"
    (source / "doc.md").write_text("line1\n" + leak)
    commit_all(source)
    before = snapshot(target)

    assert run(source, target) == 2

    err = capsys.readouterr().err
    assert "doc.md:2" in err
    for secret in (FAKE_NAME, "bob_runner", "fakehome"):
        assert secret not in err
    assert snapshot(target) == before


def test_ignored_files_are_preserved_and_not_reported(repos, capsys):
    source, target = repos
    (target / ".venv").mkdir()
    (target / ".venv" / "lib.py").write_text("v\n")
    (source / "added.txt").write_text("a\n")
    commit_all(source)

    assert run(source, target) == 0

    out = capsys.readouterr().out
    assert (target / ".venv" / "lib.py").read_text() == "v\n"
    assert ".venv" not in out


def test_add_colliding_with_ignored_file_is_rejected(repos, capsys):
    source, target = repos
    (target / ".venv").mkdir()
    (target / ".venv" / "lib.py").write_text("v\n")
    (source / ".venv").mkdir()
    (source / ".venv" / "lib.py").write_text("from source\n")
    (source / "mod.txt").write_text("changed too\n")
    git(source, "add", "-f", ".venv/lib.py")
    commit_all(source)
    before = snapshot(target)

    assert run(source, target) == 2

    assert ".venv/lib.py" in capsys.readouterr().err
    assert snapshot(target) == before


def test_long_lists_are_truncated(repos, capsys):
    source, target = repos
    baseline(source, target)
    for i in range(35):
        (source / f"n{i:02}.txt").write_text("x\n")
    commit_all(source)

    assert run(source, target) == 0

    out = capsys.readouterr().out
    assert "新增: 35" in out and "另有 5 个未列出" in out
    assert "n34.txt" not in out
