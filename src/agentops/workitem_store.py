"""工作项的磁盘存储：每项一个文件，按仓库加锁，临时文件加 os.replace 原子写。"""

from __future__ import annotations

import fcntl
import os
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from agentops import events
from agentops.paths import workitems_dir, workitems_root
from agentops.workitem_model import (
    ACTIVE_STATUSES,
    ValidationError,
    WorkItem,
    WorkItemError,
    force_status,
    from_text,
    is_valid_repo,
    parse_ref,
    to_text,
    update,
    validate,
)


def _check_repo(repo: str) -> str:
    if not is_valid_repo(repo):
        raise ValidationError(f"repo 名不合法: {repo!r}")
    return repo


def item_path(repo: str, item_id: int) -> Path:
    return workitems_dir(_check_repo(repo)) / f"{item_id:03d}.md"


@contextmanager
def repo_lock(repo: str) -> Iterator[None]:
    """同一仓库的编号分配和读改写互斥。"""
    directory = workitems_dir(_check_repo(repo))
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / ".lock", "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load(repo: str, item_id: int) -> WorkItem:
    path = item_path(repo, item_id)
    if not path.exists():
        raise WorkItemError(f"没有工作项 {repo}#{item_id}（{path}）")
    item = from_text(path.read_text(encoding="utf-8-sig"), source=str(path))
    if (item.repo, item.id) != (repo, item_id):
        raise ValidationError(f"{path} 的 front matter 写的是 {item.ref}，与文件位置不符")
    return item


def load_ref(ref: str) -> WorkItem:
    return load(*parse_ref(ref))


def save(item: WorkItem) -> WorkItem:
    path = item_path(item.repo, item.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(to_text(validate(item)), encoding="utf-8")
    os.replace(temp, path)
    return item


def _repo_dirs(repo: str | None) -> tuple[Path, ...]:
    if repo is not None:
        return (workitems_dir(_check_repo(repo)),)
    root = workitems_root()
    if not root.exists():
        return ()
    return tuple(sorted(p for p in root.iterdir() if p.is_dir()))


def _item_files(directory: Path) -> tuple[Path, ...]:
    return tuple(sorted(p for p in directory.glob("[0-9]*.md") if p.stem.isdigit()))


def list_items(repo: str | None = None, status: str | None = None) -> tuple[WorkItem, ...]:
    loaded: list[WorkItem] = []
    for directory in _repo_dirs(repo):
        if not directory.exists():
            continue
        for path in _item_files(directory):
            loaded.append(from_text(path.read_text(encoding="utf-8-sig"), source=str(path)))
    return tuple(
        sorted(
            (item for item in loaded if status is None or item.status == status),
            key=lambda item: (item.repo, item.id),
        )
    )


def next_id(repo: str) -> int:
    directory = workitems_dir(_check_repo(repo))
    if not directory.exists():
        return 1
    return max((int(p.stem) for p in _item_files(directory)), default=0) + 1


def create(
    *,
    repo: str,
    title: str,
    item_id: int | None = None,
    type: str = "feat",
    module: str = "core",
    priority: str = "P2",
    depends_on: Iterable[int] = (),
    acceptance: Iterable[str] = (),
    trigger: str = "auto",
    body: str = "",
) -> WorkItem:
    _check_repo(repo)
    with repo_lock(repo):
        item = validate(
            WorkItem(
                id=item_id if item_id is not None else next_id(repo),
                repo=repo,
                title=title,
                type=type,
                module=module,
                priority=priority,
                depends_on=tuple(int(d) for d in depends_on),
                acceptance=tuple(str(a) for a in acceptance),
                trigger=trigger,
                body=body,
            )
        )
        if item_path(repo, item.id).exists():
            raise WorkItemError(f"{item.ref} 已存在，编号分配冲突")
        return save(item)


def _emit_status(before: WorkItem, after: WorkItem, forced: bool) -> None:
    if after.status == before.status:
        return
    try:
        events.emit(
            "wi_status",
            repo=after.repo,
            item=after.id,
            session=after.session_id,
            detail={"from": before.status, "to": after.status, "forced": forced},
        )
    except OSError as exc:
        print(f"警告: {after.ref} 已保存，但 wi_status 事件写入失败: {exc}", file=sys.stderr)


def apply(item: WorkItem, *, force: bool = False, **changes: object) -> WorkItem:
    """在仓库锁内重新读取、修改、原子落盘；状态变化写 wi_status 事件。force 只给人用。"""
    with repo_lock(item.repo):
        current = load(item.repo, item.id)
        if force and "status" in changes:
            rest = {k: v for k, v in changes.items() if k != "status"}
            updated = force_status(update(current, **rest), str(changes["status"]))
        else:
            updated = update(current, **changes)
        save(updated)
    _emit_status(current, updated, force)
    return updated


def blocked_by(item: WorkItem, *, items: Iterable[WorkItem] | None = None) -> tuple[int, ...]:
    pool = {i.id: i for i in (items if items is not None else list_items(item.repo))}
    return tuple(
        dep for dep in item.depends_on if pool.get(dep) is None or pool[dep].status != "landed"
    )


def is_dispatchable(
    item: WorkItem, *, nightly: bool = False, items: Iterable[WorkItem] | None = None
) -> bool:
    if item.status != "ready" or blocked_by(item, items=items):
        return False
    return item.trigger == "auto" or nightly


def module_busy(repo: str, module: str, *, exclude_id: int | None = None) -> tuple[WorkItem, ...]:
    """同模块处于活动状态的工作项（模块锁）。"""
    return tuple(
        item
        for item in list_items(repo)
        if item.module == module and item.status in ACTIVE_STATUSES and item.id != exclude_id
    )
