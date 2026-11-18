"""PM 提案模式：PM 读研究背景和工作项清单，提出 backlog 工作项；人在看板采纳（ready）或放弃。

只写 backlog，绝不派发；同名工作项不重复提；待决定的建议数达到上限就不调用 LLM。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path, PurePosixPath

from agentops import budget, events, llm, pm
from agentops import repo as repo_tools
from agentops import workitems as wi
from agentops.config import Config
from agentops.errors import AgentopsError
from agentops.timeutil import now, now_iso
from agentops.workitem_model import PRIORITIES, TYPES, WorkItem

PROPOSED_BY = "pm"
RECENT_RESULT_DAYS = 14
RESULT_EXCERPT_CHARS = 400
TITLE_MAX_CHARS = 80
BODY_MAX_CHARS = 4000
NOTES_MAX_CHARS = 500
DEFAULT_CONTEXT_CHARS = 6000
DEFAULT_MAX_PROPOSALS = 2
DEFAULT_MAX_OPEN = 3

PROPOSAL_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "type": {"type": "string", "enum": list(TYPES)},
                    "module": {"type": "string"},
                    "priority": {"type": "string", "enum": list(PRIORITIES)},
                    "depends_on": {"type": "array", "items": {"type": "integer"}},
                    "acceptance": {"type": "array", "items": {"type": "string"}},
                    "body": {"type": "string"},
                    "reason": {"type": "string"},
                    "allow_test_edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "reason": {"type": "string"},
                            },
                            "required": ["path", "reason"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "title",
                    "type",
                    "module",
                    "priority",
                    "depends_on",
                    "acceptance",
                    "body",
                    "reason",
                ],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["proposals", "notes"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ProposalReport:
    repo: str
    created: tuple[int, ...] = ()
    rejected: tuple[str, ...] = ()  # 被拒绝的建议标题
    context_files: tuple[str, ...] = ()
    cost_usd: float = 0.0
    notes: str = ""
    skipped_reason: str = ""


def open_proposals(repo: str | None = None) -> tuple[WorkItem, ...]:
    """待人决定的 PM 建议：backlog 且 proposed_by=pm。"""
    return tuple(
        i
        for i in wi.list_items(repo, status="backlog")
        if i.extra.get("proposed_by") == PROPOSED_BY
    )


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, list | tuple) else []


def _context(config: Config, repo: str) -> tuple[str, tuple[str, ...]]:
    """仓库里的研究背景文件（人维护），读不到的跳过。"""
    root = config.repo_path(repo)
    limit = int(config.pm.get("context_chars", DEFAULT_CONTEXT_CHARS))  # type: ignore[arg-type]
    names: list[str] = []
    parts: list[str] = []
    for rel in _list(config.pm.get("context_files", ())):
        try:
            text = (root / str(rel)).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names.append(str(rel))
        parts.append(f"### {rel}\n{text[:limit]}")
    return "\n\n".join(parts) or "（没有研究背景文件）", tuple(names)


def _recent_results(repo: str, items: Sequence[WorkItem]) -> str:
    """最近合入的工作项留下的最后回复摘录：PM 据此知道上一批得出了什么。"""
    since = now() - timedelta(days=RECENT_RESULT_DAYS)
    recent = {
        e.item
        for e in events.read_events()
        if e.type == "landed" and e.repo == repo and e.item is not None and e.time >= since
    }
    excerpts = {i.id: str(i.extra.get("last_result") or "").strip() for i in items}
    lines = [
        f"- #{i.id} {i.title}：{excerpts[i.id][:RESULT_EXCERPT_CHARS]}"
        for i in items
        if i.id in recent and excerpts[i.id]
    ]
    return "\n".join(lines) or f"（最近 {RECENT_RESULT_DAYS} 天没有带结果摘录的合入）"


def _prompt(
    config: Config, repo: str, snap: pm.Snapshot, max_proposals: int
) -> tuple[str, tuple[str, ...]]:
    template = (repo_tools.TEMPLATES / "prompts" / "propose.md").read_text(encoding="utf-8")
    context, names = _context(config, repo)
    values = {
        "repo": repo,
        "max_proposals": max_proposals,
        "context_names": "、".join(names) or "无",
        "context": context,
        "items": pm.items_table(snap),
        "recent_results": _recent_results(repo, snap.items),
        "open_titles": "；".join(i.title for i in open_proposals(repo)) or "无",
    }
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template, names


def _grant_error(path: str, root: Path) -> str:
    """授权路径不合格的原因；合格返回空串。"""
    if not path.strip():
        return "allow_test_edits 有一条路径为空"
    if path != path.strip() or "\\" in path:
        return f"allow_test_edits 路径含空白或反斜杠：{path!r}"
    pure = PurePosixPath(path)
    if pure.is_absolute():
        return f"allow_test_edits 不能是绝对路径：{path}"
    if ".." in pure.parts:
        return f"allow_test_edits 不能含 ..：{path}"
    if pure.parts[0] != "tests" or len(pure.parts) < 2:
        return f"allow_test_edits 只能是 tests/ 下的文件：{path}"
    if not (root / pure).is_file():
        return f"allow_test_edits 指向的文件不存在：{path}"
    return ""


def _grants(raw: object, root: Path) -> tuple[tuple[tuple[str, str], ...], str]:
    """解析并校验 allow_test_edits；返回 ((路径, 原因), ...) 或错误。"""
    if raw is None:
        return (), ""
    if not isinstance(raw, list | tuple):
        return (), "allow_test_edits 不是列表"
    grants: list[tuple[str, str]] = []
    for entry in raw:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str):
            return (), f"allow_test_edits 条目格式不对：{entry!r}"
        path = str(entry["path"])
        error = _grant_error(path, root)
        if error:
            return (), error
        grants.append((path, str(entry.get("reason") or "").strip()))
    return tuple(grants), ""


def _validate(
    raw: Mapping[str, object], existing: Sequence[WorkItem], root: Path
) -> tuple[dict[str, object] | None, str]:
    """一条建议能不能变成工作项；不能就给出原因。"""
    title = str(raw.get("title") or "").strip()[:TITLE_MAX_CHARS]
    if not title:
        return None, "标题为空"
    live = [i for i in existing if i.status != "dropped"]
    if title.casefold() in {i.title.strip().casefold() for i in live}:
        return None, "同名工作项已存在"
    kind = str(raw.get("type") or "")
    if kind not in TYPES:
        return None, f"type 不合法：{kind!r}"
    priority = str(raw.get("priority") or "")
    if priority not in PRIORITIES:
        return None, f"priority 不合法：{priority!r}"
    module = str(raw.get("module") or "").strip()
    if not module:
        return None, "module 为空"
    acceptance = tuple(str(a).strip() for a in _list(raw.get("acceptance")) if str(a).strip())
    if not acceptance:
        return None, "没有验收条目"
    try:
        depends = tuple(int(d) for d in _list(raw.get("depends_on")))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None, "depends_on 不是整数列表"
    if any(d not in {i.id for i in live} for d in depends):
        return None, "depends_on 引用了不存在的工作项"
    grants, grant_error = _grants(raw.get("allow_test_edits"), root)
    if grant_error:
        return None, grant_error
    spec = {
        "title": title,
        "type": kind,
        "module": module,
        "priority": priority,
        "depends_on": depends,
        "acceptance": acceptance,
        "body": str(raw.get("body") or "")[:BODY_MAX_CHARS],
        "reason": str(raw.get("reason") or "").strip(),
        "allow_test_edits": grants,
    }
    return spec, ""


def _create(repo: str, spec: Mapping[str, object]) -> WorkItem:
    reason = str(spec["reason"])
    grants: tuple[tuple[str, str], ...] = spec["allow_test_edits"]  # type: ignore[assignment]
    grant_note = "".join(
        f"\n授权修改已有测试 {path}：{why or '（未给原因）'}" for path, why in grants
    )
    body = (
        f"{spec['body']}\n\n---\n"
        f"PM 提案（{now_iso()[:16]}）。理由：{reason or '（未给）'}{grant_note}\n"
        "采纳前请核对验收条目是否可测；采纳（看板「采纳」或 `ao wi set <ref> --status ready`）后"
        "由 steward 派发。"
    )
    item = wi.create(
        repo=repo,
        title=str(spec["title"]),
        type=str(spec["type"]),
        module=str(spec["module"]),
        priority=str(spec["priority"]),
        depends_on=spec["depends_on"],  # type: ignore[arg-type]
        acceptance=spec["acceptance"],  # type: ignore[arg-type]
        body=body,
    )
    extra: dict[str, object] = {}
    if grants:
        extra["allow_test_edits"] = [path for path, _ in grants]
    item = wi.apply(item, proposed_by=PROPOSED_BY, proposed_at=now_iso(), pm_reason=reason, **extra)
    events.emit(
        "pm_proposed",
        repo=repo,
        item=item.id,
        detail={"title": item.title, "module": item.module, "reason": reason},
    )
    return item


def propose(config: Config, repo: str) -> ProposalReport:
    """一次 LLM 调用，把合法的建议写成 backlog 工作项。失败只记事件，不抛。"""
    max_open = int(config.pm.get("max_open", DEFAULT_MAX_OPEN))  # type: ignore[arg-type]
    waiting = len(open_proposals(repo))
    if waiting >= max_open:
        reason = f"待决定的 PM 建议已有 {waiting} 条（上限 {max_open}），本次不提"
        events.emit("pm_propose_skipped", repo=repo, detail={"reason": reason})
        return ProposalReport(repo, skipped_reason=reason)
    max_proposals = int(config.pm.get("max_proposals", DEFAULT_MAX_PROPOSALS))  # type: ignore[arg-type]
    snap = pm.snapshot(config, repo)
    prompt, names = _prompt(config, repo, snap, max_proposals)
    try:
        result = llm.call_structured(
            prompt, PROPOSAL_SCHEMA, config=config, role="pm", label="pm_propose", repo=repo
        )
    except (llm.LlmError, budget.BudgetExceeded) as exc:
        events.emit("pm_propose_error", repo=repo, detail={"error": str(exc)})
        return ProposalReport(repo, context_files=names, skipped_reason=f"LLM 调用失败：{exc}")
    existing = list(snap.items)
    created: list[int] = []
    rejected: dict[str, str] = {}
    for raw in _list(result.data.get("proposals")):
        if not isinstance(raw, Mapping):
            continue
        if len(created) >= max_proposals or waiting + len(created) >= max_open:
            break
        spec, why = _validate(raw, existing, config.repo_path(repo))
        if spec is None:
            rejected[str(raw.get("title") or "")[:TITLE_MAX_CHARS]] = why
            continue
        item = _create(repo, spec)
        existing.append(item)
        created.append(item.id)
    notes = str(result.data.get("notes") or "")[:NOTES_MAX_CHARS]
    events.emit(
        "pm_propose_run",
        repo=repo,
        detail={
            "created": created,
            "rejected": rejected,
            "cost_usd": result.cost_usd,
            "notes": notes,
        },
    )
    return ProposalReport(repo, tuple(created), tuple(rejected), names, result.cost_usd, notes)


def adopt(repo: str, item_id: int, *, by: str) -> WorkItem:
    """人采纳一条建议：backlog → ready，之后由 steward 按常规派发。"""
    item = wi.load(repo, item_id)
    if item.extra.get("proposed_by") != PROPOSED_BY or item.status != "backlog":
        raise AgentopsError(f"{item.ref} 不是待决定的 PM 建议（状态 {item.status}）")
    adopted = wi.apply(item, status="ready", adopted_at=now_iso(), adopted_by=by)
    events.emit("proposal_adopted", repo=repo, item=item_id, detail={"by": by})
    return adopted
