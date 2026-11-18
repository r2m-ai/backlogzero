"""工作项的数据模型、校验、状态机和文本格式。纯函数，不碰磁盘。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from types import MappingProxyType

import yaml

from agentops.errors import AgentopsError

STATUSES = (
    "backlog",
    "ready",
    "in_progress",
    "queued",
    "testing",
    "landed",
    "returned",
    "needs_human",
    "dropped",
)
ACTIVE_STATUSES = frozenset({"in_progress", "queued", "testing", "returned", "needs_human"})
TYPES = ("feat", "fix", "chore")
PRIORITIES = ("P0", "P1", "P2", "P3")
TRIGGERS = ("auto", "nightly")
FRONT_MATTER_KEYS = (
    "id",
    "repo",
    "title",
    "type",
    "module",
    "priority",
    "status",
    "depends_on",
    "acceptance",
    "trigger",
    "session_id",
    "branch",
)
TRANSITIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "backlog": frozenset({"ready", "dropped"}),
        "ready": frozenset({"in_progress", "backlog", "dropped"}),
        "in_progress": frozenset({"queued", "returned", "needs_human", "dropped"}),
        "queued": frozenset({"testing", "returned", "needs_human", "dropped"}),
        "testing": frozenset({"landed", "returned", "needs_human", "queued", "dropped"}),
        "returned": frozenset({"in_progress", "queued", "needs_human", "dropped"}),
        "needs_human": frozenset({"queued", "returned", "dropped"}),
        "landed": frozenset(),
        "dropped": frozenset({"backlog"}),
    }
)
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_REF_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)#(\d+)$")
_FRONT_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)


class WorkItemError(AgentopsError):
    pass


class ValidationError(WorkItemError):
    pass


class InvalidTransition(WorkItemError):
    pass


class SpecNotReady(WorkItemError):
    pass


@dataclass(frozen=True)
class WorkItem:
    id: int
    repo: str
    title: str
    type: str = "feat"
    module: str = "core"
    priority: str = "P2"
    status: str = "backlog"
    depends_on: tuple[int, ...] = ()
    acceptance: tuple[str, ...] = ()
    trigger: str = "auto"
    session_id: str | None = None
    branch: str | None = None
    body: str = ""
    extra: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.id}"

    @property
    def is_spec_ready(self) -> bool:
        return any(line.strip() for line in self.acceptance)


EDITABLE_FIELDS = frozenset(
    f.name for f in fields(WorkItem) if f.name not in {"id", "repo", "status", "extra"}
)


def is_valid_repo(name: str) -> bool:
    return bool(REPO_RE.match(name))


def validate(item: WorkItem) -> WorkItem:
    problems: list[str] = []
    if item.id < 1:
        problems.append("id 必须 ≥ 1")
    if not is_valid_repo(item.repo):
        problems.append(f"repo 名不合法: {item.repo!r}")
    if not item.title.strip():
        problems.append("title 不能为空")
    if not item.module.strip():
        problems.append("module 不能为空")
    for name, value, allowed in (
        ("type", item.type, TYPES),
        ("priority", item.priority, PRIORITIES),
        ("status", item.status, STATUSES),
        ("trigger", item.trigger, TRIGGERS),
    ):
        if value not in allowed:
            problems.append(f"{name} 必须是 {'/'.join(allowed)}，得到 {value!r}")
    if item.id in item.depends_on:
        problems.append("不能依赖自己")
    if problems:
        raise ValidationError("; ".join(problems))
    return item


def parse_ref(ref: str) -> tuple[str, int]:
    match = _REF_RE.match(ref.strip())
    if not match:
        raise ValidationError(f"工作项编号格式应为 <repo>#<id>，例如 quant-lab#4，得到 {ref!r}")
    return match.group(1), int(match.group(2))


def to_text(item: WorkItem) -> str:
    front = {
        "id": item.id,
        "repo": item.repo,
        "title": item.title,
        "type": item.type,
        "module": item.module,
        "priority": item.priority,
        "status": item.status,
        "depends_on": list(item.depends_on),
        "acceptance": list(item.acceptance),
        "trigger": item.trigger,
        "session_id": item.session_id,
        "branch": item.branch,
        **dict(item.extra),
    }
    front_text = yaml.safe_dump(
        front, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    body = item.body if not item.body or item.body.endswith("\n") else item.body + "\n"
    return f"---\n{front_text}---\n{body}"


def _scalar(data: Mapping[str, object], key: str, default: str | None = None) -> str | None:
    """取字符串字段。None 用默认值；bool、列表、字典都算手改坏了。"""
    value = data.get(key)
    if value is None:
        return default
    if isinstance(value, bool | list | dict):
        raise ValidationError(f"{key} 必须是字符串，得到 {value!r}")
    return str(value)


def _required(data: Mapping[str, object], key: str) -> str:
    value = _scalar(data, key)
    if value is None or not value.strip():
        raise ValidationError(f"{key} 缺失或为空")
    return value


def _items(data: Mapping[str, object], key: str) -> tuple[object, ...]:
    value = data.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValidationError(f"{key} 必须是列表，得到 {value!r}")
    return tuple(value)


def _int_item(value: object, key: str) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{key} 的元素必须是整数，得到 {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    raise ValidationError(f"{key} 的元素必须是整数，得到 {value!r}")


def _str_item(value: object, key: str) -> str:
    if value is None or isinstance(value, bool | list | dict):
        raise ValidationError(f"{key} 的元素必须是字符串，得到 {value!r}")
    return str(value)


def from_text(text: str, *, source: str = "<text>") -> WorkItem:
    match = _FRONT_RE.match(text)
    if not match:
        raise ValidationError(f"{source}: 缺少 YAML front matter")
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ValidationError(f"{source}: front matter 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError(f"{source}: front matter 必须是键值对")
    try:
        item = WorkItem(
            id=_int_item(data.get("id"), "id"),
            repo=_required(data, "repo"),
            title=_required(data, "title"),
            type=_scalar(data, "type", "feat") or "feat",
            module=_scalar(data, "module", "core") or "core",
            priority=_scalar(data, "priority", "P2") or "P2",
            status=_scalar(data, "status", "backlog") or "backlog",
            depends_on=tuple(_int_item(d, "depends_on") for d in _items(data, "depends_on")),
            acceptance=tuple(_str_item(a, "acceptance") for a in _items(data, "acceptance")),
            trigger=_scalar(data, "trigger", "auto") or "auto",
            session_id=_scalar(data, "session_id"),
            branch=_scalar(data, "branch"),
            body=match.group(2),
            extra=MappingProxyType({k: v for k, v in data.items() if k not in FRONT_MATTER_KEYS}),
        )
    except ValidationError as exc:
        raise ValidationError(f"{source}: {exc}") from exc
    return validate(item)


def transition(item: WorkItem, new_status: str) -> WorkItem:
    if new_status not in STATUSES:
        raise ValidationError(f"未知状态 {new_status!r}，可用: {'/'.join(STATUSES)}")
    if new_status == item.status:
        return item
    if new_status not in TRANSITIONS[item.status]:
        raise InvalidTransition(f"{item.ref}: 不能从 {item.status} 直接到 {new_status}")
    if new_status == "ready" and not item.is_spec_ready:
        raise SpecNotReady(f"{item.ref}: acceptance 为空，规格未就绪，不能进入 ready")
    return replace(item, status=new_status)


def _normalize(changes: Mapping[str, object]) -> dict[str, object]:
    normalized = dict(changes)
    if "depends_on" in normalized:
        raw = normalized["depends_on"]
        if not isinstance(raw, list | tuple):
            raise ValidationError(f"depends_on 必须是列表，得到 {raw!r}")
        normalized["depends_on"] = tuple(_int_item(d, "depends_on") for d in raw)
    if "acceptance" in normalized:
        raw = normalized["acceptance"]
        if not isinstance(raw, list | tuple):
            raise ValidationError(f"acceptance 必须是列表，得到 {raw!r}")
        normalized["acceptance"] = tuple(_str_item(a, "acceptance") for a in raw)
    for key in ("title", "type", "module", "priority", "trigger", "body"):
        if key in normalized and not isinstance(normalized[key], str):
            raise ValidationError(f"{key} 必须是字符串，得到 {normalized[key]!r}")
    for key in ("session_id", "branch"):
        if (
            key in normalized
            and normalized[key] is not None
            and not isinstance(normalized[key], str)
        ):
            raise ValidationError(f"{key} 必须是字符串或 None，得到 {normalized[key]!r}")
    return normalized


def update(item: WorkItem, **changes: object) -> WorkItem:
    """返回修改后的新工作项。status 走状态机；未知键进 extra；值为 None 的未知键被删除。"""
    if "id" in changes or "repo" in changes:
        raise ValidationError("id 和 repo 不能修改")
    status = changes.pop("status", None)
    known = {k: v for k, v in changes.items() if k in EDITABLE_FIELDS}
    unknown = {k: v for k, v in changes.items() if k not in EDITABLE_FIELDS}
    merged_extra = {k: v for k, v in {**item.extra, **unknown}.items() if v is not None}
    result = replace(item, **_normalize(known), extra=MappingProxyType(merged_extra))
    if status is not None:
        result = transition(result, str(status))
    return validate(result)


def force_status(item: WorkItem, new_status: str) -> WorkItem:
    """跳过状态机直接设状态，只给人用。"""
    return validate(replace(item, status=new_status))
