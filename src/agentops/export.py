"""ao export：把源仓库 HEAD 的导出内容同步到公开仓库的本机副本（只准备，不提交不推送）。"""

from __future__ import annotations

import io
import os
import stat
import subprocess
import tarfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agentops.errors import AgentopsError

GITLINK_MODE = "160000"
MAX_LISTED = 30


@dataclass(frozen=True)
class Entry:
    """导出内容里的一个文件：普通文件带内容和可执行位，符号链接带目标。"""

    data: bytes
    executable: bool = False
    symlink: bool = False


@dataclass(frozen=True)
class ExportResult:
    source_sha: str
    added: tuple[str, ...]
    modified: tuple[str, ...]
    deleted: tuple[str, ...]

    @property
    def is_unchanged(self) -> bool:
        return not (self.added or self.modified or self.deleted)


def _git(repo: Path, *args: str) -> bytes:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    except OSError as exc:
        raise AgentopsError(f"无法运行 git: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()
        raise AgentopsError(f"git {' '.join(args)} 失败（{repo}）: {detail}")
    return proc.stdout


def _check_repo(path: Path, label: str) -> None:
    if not path.is_dir():
        raise AgentopsError(f"{label}不是目录: {path}")
    if not (path / ".git").exists():
        raise AgentopsError(f"{label}不是 git 仓库（缺少 .git）: {path}")
    toplevel = Path(_git(path, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if toplevel != path:
        raise AgentopsError(f"{label}不是仓库根目录: {path}")
    if _git(path, "status", "--porcelain").strip():
        raise AgentopsError(f"{label}工作区不干净，请先提交或清理: {path}")


def _check_disjoint(source: Path, target: Path) -> None:
    if source == target or source in target.parents or target in source.parents:
        raise AgentopsError(f"源和目标不能相同或互相包含: {source} / {target}")


def _safe_rel(name: str) -> str:
    parts = PurePosixPath(name).parts
    if not parts or PurePosixPath(name).is_absolute() or ".." in parts or parts[0] == ".git":
        raise AgentopsError(f"导出内容里有不安全的路径: {name!r}")
    return "/".join(parts)


def read_export(source: Path) -> dict[str, Entry]:
    """git archive HEAD 的内容读进内存（自动遵守 export-ignore），不落盘。"""
    raw = _git(source, "archive", "--format=tar", "HEAD")
    entries: dict[str, Entry] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
        for member in tar:
            if member.isdir():
                continue
            rel = _safe_rel(member.name)
            if member.issym():
                entries[rel] = Entry(member.linkname.encode(), symlink=True)
                continue
            handle = tar.extractfile(member)
            if not member.isfile() or handle is None:
                raise AgentopsError(f"导出内容里有不支持的文件类型: {rel}")
            entries[rel] = Entry(handle.read(), executable=bool(member.mode & 0o111))
    return entries


def scan_hits(entries: Mapping[str, Entry], terms: Iterable[str]) -> list[str]:
    """返回 `文件:行号` 列表；只报位置，不含命中的词。"""
    needles = [t.encode() for t in terms if t]
    hits: list[str] = []
    for rel, entry in sorted(entries.items()):
        if entry.symlink:
            continue
        for lineno, line in enumerate(entry.data.split(b"\n"), start=1):
            if any(n in line for n in needles):
                hits.append(f"{rel}:{lineno}")
    return hits


def _tracked_files(target: Path) -> list[str]:
    out = _git(target, "ls-files", "-s", "-z").decode()
    paths = []
    for record in filter(None, out.split("\0")):
        meta, _, path = record.partition("\t")
        if meta.split()[0] != GITLINK_MODE:
            paths.append(path)
    return paths


def _current_entry(target: Path, rel: str) -> Entry | None:
    path = target / rel
    if path.is_symlink():
        return Entry(os.readlink(path).encode(), symlink=True)
    if not path.is_file():
        return None
    return Entry(path.read_bytes(), executable=bool(path.stat().st_mode & stat.S_IXUSR))


def _inside(target: Path, rel: str) -> Path:
    """解析父目录后确认落在 target 之内且不碰 .git；返回绝对路径。"""
    path = target / rel
    parent = path.parent.resolve()
    if parent != target and target not in parent.parents:
        raise AgentopsError(f"路径越出目标目录: {rel}")
    if ".git" in path.relative_to(target).parts[:1]:
        raise AgentopsError(f"拒绝改动 .git: {rel}")
    return parent / path.name


def _blocking_paths(target: Path, added: Iterable[str], deleted: set[str]) -> list[str]:
    """要新增的路径若已存在（被忽略的文件），或某级父路径不是目录，就算冲突。"""
    blocked = []
    for rel in added:
        path = target / rel
        ancestors = [target / p for p in list(PurePosixPath(rel).parents)[:-1]]
        stuck = any(
            os.path.lexists(a) and (a.is_symlink() or not a.is_dir()) and str(a) not in deleted
            for a in ancestors
        )
        if stuck or os.path.lexists(path):
            blocked.append(rel)
    return sorted(blocked)


def _write(path: Path, entry: Entry) -> None:
    if os.path.lexists(path):
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    if entry.symlink:
        os.symlink(entry.data.decode(), path)
        return
    path.write_bytes(entry.data)
    path.chmod(0o755 if entry.executable else 0o644)


def _prune_empty_parents(target: Path, path: Path) -> None:
    for parent in path.parents:
        if parent == target or target not in parent.parents:
            return
        try:
            parent.rmdir()
        except OSError:
            return


def run_export(
    source: Path,
    target: Path,
    *,
    redact: Iterable[str],
    home: Path,
    username: str,
) -> ExportResult:
    source, target = source.resolve(), target.resolve()
    _check_repo(source, "源")
    _check_repo(target, "目标")
    _check_disjoint(source, target)

    entries = read_export(source)
    terms = [*redact, str(home) if str(home) not in ("", "/") else "", username]
    hits = scan_hits(entries, terms)
    if hits:
        raise AgentopsError(
            "导出内容命中 redact 词、家目录路径或用户名，已拒绝（只列位置）:\n  "
            + "\n  ".join(hits)
        )

    tracked = _tracked_files(target)
    tracked_set = set(tracked)
    added = sorted(set(entries) - tracked_set)
    deleted = sorted(tracked_set - set(entries))
    modified = sorted(
        p for p in tracked_set & set(entries) if _current_entry(target, p) != entries[p]
    )

    blocked = _blocking_paths(target, added, {str(target / p) for p in deleted})
    if blocked:
        raise AgentopsError(
            "要新增的路径在目标里已存在（可能是被 .gitignore 忽略的文件），已拒绝，请人工处理:\n  "
            + "\n  ".join(blocked)
        )
    destinations = {rel: _inside(target, rel) for rel in (*added, *modified, *deleted)}

    for rel in deleted:
        destinations[rel].unlink()
        _prune_empty_parents(target, destinations[rel])
    for rel in (*added, *modified):
        _write(destinations[rel], entries[rel])

    sha = _git(source, "rev-parse", "--short", "HEAD").decode().strip()
    return ExportResult(sha, tuple(added), tuple(modified), tuple(deleted))
