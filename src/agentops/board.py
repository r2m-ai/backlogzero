"""看板（C10）：标准库 HTTP 服务，只监听 127.0.0.1，一个页面 30 秒自刷新。

审批和确认放弃是唯一的人类审批通道（除了人在终端 `ao approve`）。防伪造：
- 页面里嵌一个启动时随机生成的令牌，POST 必须带上；
- 请求的 Host 必须是 127.0.0.1:<port>，Origin 必须是 http://127.0.0.1:<port>；
- 审批绑定 (工作项, SHA)：表单里的 sha 必须等于工作项当前的 approval_sha。
"""

from __future__ import annotations

import json
import secrets
import sys
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from agentops import (
    approvals,
    board_data,
    board_style,
    board_view,
    clones,
    events,
    pm_propose,
    queue,
)
from agentops import workitems as wi
from agentops.config import Config
from agentops.errors import AgentopsError
from agentops.timeutil import now_iso

HOST = "127.0.0.1"
REFRESH_SECONDS = 30
MAX_BODY = 4096
MAX_MESSAGE = 200  # 跳转回首页时带回的提示长度上限


class BoardState:
    def __init__(self, config: Config, port: int) -> None:
        self.config = config
        self.port = port
        self.token = secrets.token_hex(16)
        self.started = now_iso()

    @property
    def origin(self) -> str:
        return f"http://{HOST}:{self.port}"

    @property
    def host_header(self) -> str:
        return f"{HOST}:{self.port}"


def check_request_origin(state: BoardState, headers: Mapping[str, str]) -> str | None:
    """返回拒绝原因；None 表示通过。"""
    host = headers.get("Host", "")
    origin = headers.get("Origin", "")
    if host != state.host_header:
        return f"Host 不是 {state.host_header}"
    if origin != state.origin:
        return f"Origin 不是 {state.origin}"
    return None


def handle_action(
    state: BoardState, path: str, form: Mapping[str, str], headers: Mapping[str, str]
) -> tuple[int, str]:
    """处理 POST。返回 (状态码, 说明)。所有拒绝都写 board_rejected 事件。"""
    reason = check_request_origin(state, headers)
    if reason is None and not secrets.compare_digest(form.get("token", ""), state.token):
        reason = "令牌不对"
    if reason is not None:
        events.emit(
            "board_rejected",
            detail={
                "path": path,
                "reason": reason,
                "origin": headers.get("Origin", ""),
                "host": headers.get("Host", ""),
            },
        )
        return HTTPStatus.FORBIDDEN, reason
    try:
        repo, item_id = wi.parse_ref(form.get("ref", ""))
        item = wi.load(repo, item_id)
        if path == "/approve":
            sha = form.get("sha", "")
            current = str(item.extra.get("approval_sha") or "")
            if not sha or sha != current or item.status != "needs_human":
                return (
                    HTTPStatus.CONFLICT,
                    "审批的 SHA 与工作项当前等待审批的 SHA 不一致，页面可能过期，请刷新",
                )
            if not board_data.is_approval_gate(item):
                return (
                    HTTPStatus.CONFLICT,
                    "该项不是审批门造成的 needs_human（测试失败/冲突/退回超限），批准无效；"
                    "请查看原因后选择「重新入队」或「放弃」",
                )
            approvals.record(repo, item_id, sha, by="board", note="board button")
            return HTTPStatus.OK, f"已批准 {item.ref} @ {sha[:7]}"
        if path == "/requeue":
            return _requeue(item)
        if path == "/adopt":
            adopted = pm_propose.adopt(repo, item_id, by="board")
            return HTTPStatus.OK, f"已采纳 {adopted.ref}，等待 steward 派发"
        if path == "/drop":
            dropped = wi.apply(item, status="dropped")
            events.emit("dropped", repo=repo, item=item_id, detail={"by": "board"})
            # 放弃后克隆也要删（审核人决定：合入或放弃后删除克隆）
            clones.remove_clone(state.config, dropped, reason="dropped")
            return HTTPStatus.OK, f"已放弃 {item.ref}"
    except AgentopsError as exc:
        return HTTPStatus.BAD_REQUEST, str(exc)
    return HTTPStatus.NOT_FOUND, "未知操作"


def _requeue(item: wi.WorkItem) -> tuple[int, str]:
    """人确认过失败原因后，把非审批门的 needs_human 项重新送入合并队列。"""
    sha = str(item.extra.get("approval_sha") or "")
    clone_path = str(item.extra.get("clone_path") or "")
    branch = str(item.branch or item.extra.get("branch") or "")
    if item.status != "needs_human" or board_data.is_approval_gate(item):
        return HTTPStatus.CONFLICT, "只有测试失败/冲突/退回超限的 needs_human 项可以重新入队"
    if not (sha and clone_path and branch):
        return HTTPStatus.CONFLICT, "工作项缺少分支、克隆或 SHA 记录，无法重新入队"
    request = queue.new_request(
        repo=item.repo,
        item=item.id,
        branch=branch,
        sha=sha,
        clone_path=clone_path,
        session=item.session_id,
    )
    queue.submit_inbox(request)
    wi.apply(item, status="queued")
    events.emit(
        "requeued_by_human", repo=item.repo, item=item.id, detail={"sha": sha, "by": "board"}
    )
    return HTTPStatus.OK, f"已重新入队 {item.ref}"


def render_page(state: BoardState, message: str = "") -> str:
    return board_view.render(
        board_data.all_panels(state.config),
        token=state.token,
        now_text=now_iso(),
        brief=board_data.latest_brief(),
        message=message,
        refresh_seconds=REFRESH_SECONDS,
    )


class BoardHandler(BaseHTTPRequestHandler):
    state: BoardState

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        print(f"board: {self.address_string()} {format % args}", file=sys.stderr)

    def _send(self, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        path = parts.path
        if path in ("/", "/index.html"):
            message = parse_qs(parts.query).get("m", [""])[0][:MAX_MESSAGE]
            self._send(HTTPStatus.OK, render_page(self.state, message))
        elif path == "/api/state":
            payload = [
                {
                    "key": p.key,
                    "title": p.title,
                    "rows": [dict(r) for r in p.rows],
                    "note": p.note,
                    "error": p.error,
                }
                for p in board_data.all_panels(self.state.config)
            ]
            self._send(
                HTTPStatus.OK,
                json.dumps(payload, ensure_ascii=False, default=str),
                "application/json; charset=utf-8",
            )
        elif path == board_view.SCRIPT_PATH:
            self._send(HTTPStatus.OK, board_style.SCRIPT, "text/javascript; charset=utf-8")
        elif path == "/healthz":
            self._send(HTTPStatus.OK, "ok", "text/plain; charset=utf-8")
        else:
            self._send(HTTPStatus.NOT_FOUND, "not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_BODY:
            self._send(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too large", "text/plain; charset=utf-8"
            )
            return
        raw = self.rfile.read(length).decode("utf-8", "replace")
        form = {k: v[0] for k, v in parse_qs(raw).items()}
        status, message = handle_action(self.state, self.path, form, dict(self.headers))
        if status == HTTPStatus.OK:
            # POST 后跳转回首页（PRG）：否则页面停在 /approve，30 秒自动刷新会变成 404
            self._redirect("/?m=" + quote(message))
        else:
            self._send(status, f"{status} {message}", "text/plain; charset=utf-8")


def make_server(config: Config, port: int) -> ThreadingHTTPServer:
    state = BoardState(config, port)
    handler = type("Handler", (BoardHandler,), {"state": state})
    server = ThreadingHTTPServer((HOST, port), handler)
    server.daemon_threads = True
    state.port = server.server_address[1]  # port 0 时取系统分配的端口
    return server


def serve(config: Config, port: int | None = None) -> None:
    chosen = int(port if port is not None else config.board.get("port", 8749))  # type: ignore[arg-type]
    server = make_server(config, chosen)
    events.emit("board_start", detail={"port": chosen})
    print(f"看板：http://{HOST}:{chosen}（只监听本机）", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
