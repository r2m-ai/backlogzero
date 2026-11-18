"""从一条 shell 命令里找"会被写的路径"，给 protect_tests 在 Codex 下做保守检查用。

Codex 的模型既能用 apply_patch 也能直接跑 shell 改文件。这里只认看得出目标的写法：
重定向、rm / mv / cp / tee / truncate / chmod / patch、sed -i / perl -i、
git checkout / restore / rm / mv、dd of=、find … -delete / -exec；会跟踪 `cd <目录>`，
会进到 `bash -c '…'` / `eval …` 里面看。看不出来的（python -c 之类的解释器写文件、xargs、
git apply、变量里的路径）由 steward 合入时的 existing_test_modified 与受保护路径规则兜底。

原则是宁可误拦不漏拦：解析不了的行按粗切词再看一遍；命令太长或嵌套太深直接报 Unanalyzable，
由调用方拒绝。按行顺序扫描，没有会回溯的正则。
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Iterator

MAX_COMMAND_CHARS = 65536
MAX_DEPTH = 3
REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>"})
SEPARATORS = frozenset({"|", "||", "&&", ";", "&", "|&", ";;"})
OPEN_GROUP = frozenset({"(", "{"})
CLOSE_GROUP = frozenset({")", "}"})
HEREDOC = "<<"
WRITES_EVERY_ARG = frozenset(
    {"rm", "tee", "truncate", "unlink", "rmdir", "shred", "chmod", "chown", "patch"}
)
WRITES_DESTINATION = frozenset({"cp", "install", "ln", "rsync"})
MOVES = frozenset({"mv"})
TARGET_DIR_OPTIONS = ("-t", "--target-directory")
IN_PLACE_EDITORS = frozenset({"sed", "perl", "gsed"})
GIT_WRITE_SUBCOMMANDS = frozenset({"checkout", "restore", "rm", "mv"})
GIT_OPTIONS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree"})
SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
CHANGE_DIR = frozenset({"cd", "pushd"})
FIND_WRITE_ACTIONS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir"})
WRAPPERS = frozenset({"sudo", "command", "env", "time", "nohup", "exec", "builtin", "nice"})
KEYWORDS = frozenset(
    {"if", "then", "else", "elif", "do", "while", "until", "!"}
)  # 后面跟的才是命令
WRAPPER_OPTIONS_WITH_VALUE = frozenset({"-u", "--unset", "-C", "--chdir", "-g", "-n"})
GLOB_CHARS = re.compile(r"[*?\[]")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
CRUDE_TOKEN = re.compile(r"[()|&;<>]+|[^\s()|&;<>]+")
HEREDOC_WORD = re.compile(r"^-?\w+$")
PUNCTUATION_ONLY = re.compile(r"^[()|&;<>]+$")
PAREN_SPLIT = re.compile(r"[()]|[^()]+")


class Unanalyzable(Exception):
    """命令没法可靠地检查（太长、嵌套太深）。调用方应当拒绝。"""


def _quote_state(state: str | None, line: str) -> str | None:
    """扫一行，返回行尾状态：None 正常；引号字符表示还在引号里；反斜杠表示行尾续行。"""
    quote = state if state in ("'", '"') else None
    index, size = 0, len(line)
    while index < size:
        char = line[index]
        if quote == "'":
            if char == "'":
                quote = None
        elif quote == '"':
            if char == "\\":
                index += 1
            elif char == '"':
                quote = None
        elif char == "\\":
            if index == size - 1:
                return "\\"
            index += 1
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            break  # 注释到行尾
        index += 1
    return quote


def _tokens(text: str) -> list[str] | None:
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        raw = list(lexer)
    except ValueError:
        return None
    tokens: list[str] = []
    for token in raw:
        # shlex 把连着的标点并成一个词（`);`、`((`），把括号单独拆出来才认得出子 shell 的边界
        if PUNCTUATION_ONLY.match(token) and ("(" in token or ")" in token):
            tokens += PAREN_SPLIT.findall(token)
        else:
            tokens.append(token)
    return tokens


def _heredoc_words(tokens: list[str], line: str) -> list[str]:
    """这一行开了哪些 heredoc（按出现顺序给出结束词）。

    只认真正的 `<<` 操作符：引号里的 `<<A` 是普通字符串，`$((1<<2))` 是移位。
    """
    if "((" in line:
        return []
    words = []
    for index, token in enumerate(tokens[:-1]):
        candidate = tokens[index + 1]
        if (
            token == HEREDOC
            and HEREDOC_WORD.match(candidate)
            and not candidate.lstrip("-").isdigit()
        ):
            words.append(candidate.lstrip("-"))
    return words


def _logical_lines(command: str) -> Iterator[list[str]]:
    """按 shell 的读法顺序切成逻辑行并切词：引号没闭合或行尾反斜杠就接下一行，heredoc 正文跳过。

    切不了词的行（引号到最后也没闭合等）按粗切词给出，而且不再跳它的 heredoc 正文。
    """
    physical = command.split("\n")
    index = 0
    while index < len(physical):
        parts = [physical[index]]
        state = _quote_state(None, physical[index])
        index += 1
        while state is not None and index < len(physical):
            if state == "\\":
                parts[-1] = parts[-1][:-1]
            state = _quote_state(state if state != "\\" else None, physical[index])
            parts.append(physical[index])
            index += 1
        line = "\n".join(parts)
        tokens = _tokens(line)
        if tokens is None:
            yield CRUDE_TOKEN.findall(line)
            continue
        yield tokens
        for word in _heredoc_words(tokens, line):
            while index < len(physical):
                body = physical[index]
                index += 1
                if body.strip() == word:
                    break


def _redirect_targets(words: list[str]) -> tuple[list[str], list[str]]:
    """返回 (重定向目标, 去掉重定向后的词)。`2>&1` 这类复制描述符不算写文件。"""
    targets: list[str] = []
    rest: list[str] = []
    skip = False
    for index, word in enumerate(words):
        if skip:
            skip = False
            continue
        if word in REDIRECTS:
            if index + 1 < len(words):
                targets.append(words[index + 1])
                skip = True
            continue
        rest.append(word)
    return targets, rest


def _strip_prefix(words: list[str]) -> list[str]:
    """去掉前面的 `VAR=值`、if / then / do 这类关键字和 sudo / env / time 这类包装（连同选项）。"""
    index = 0
    while index < len(words):
        word = words[index]
        if ASSIGNMENT.match(word) or word in KEYWORDS:
            index += 1
        elif word in WRAPPERS:
            index += 1
            while index < len(words) and words[index].startswith("-"):
                index += 2 if words[index] in WRAPPER_OPTIONS_WITH_VALUE else 1
        else:
            break
    return words[index:]


def _plain_args(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _is_in_place(args: list[str]) -> bool:
    return any(a.startswith("-") and not a.startswith("--") and "i" in a[1:] for a in args) or any(
        a.startswith("--in-place") for a in args
    )


def _git_targets(args: list[str]) -> list[str]:
    index = 0
    while index < len(args) and args[index].startswith("-"):
        index += 2 if args[index] in GIT_OPTIONS_WITH_VALUE else 1
    if index >= len(args) or args[index] not in GIT_WRITE_SUBCOMMANDS:
        return []
    return _plain_args(args[index + 1 :])


def _destinations(args: list[str]) -> tuple[list[str], list[str]]:
    """cp / mv 这类命令的 (源, 实际写到的路径)。

    目的地是目录（`-t <目录>`，或以 / 结尾）时写到的是"目录/源文件名"：
    往 tests/ 里放一个新文件是允许的，不能因为 tests/ 本身已存在就拦。
    """
    directory: str | None = None
    plain: list[str] = []
    skip = False
    for index, arg in enumerate(args):
        if skip:
            skip = False
        elif arg in TARGET_DIR_OPTIONS and index + 1 < len(args):
            directory, skip = args[index + 1], True
        elif arg.startswith("--target-directory="):
            directory = arg.split("=", 1)[1]
        elif not arg.startswith("-"):
            plain.append(arg)
    if directory is None:
        if len(plain) < 2:
            return plain, []
        *sources, destination = plain
        if not destination.endswith("/"):
            return sources, [destination]
        directory = destination
    else:
        sources = plain
    return sources, [os.path.join(directory, os.path.basename(s.rstrip("/"))) for s in sources]


def _find_targets(args: list[str]) -> list[str]:
    """find 带 -delete / -exec 时，起始目录都算会被写。"""
    if not any(a in FIND_WRITE_ACTIONS for a in args):
        return []
    starts = []
    for arg in args:
        if arg.startswith("-") or arg in ("(", "!"):
            break
        starts.append(arg)
    return starts or ["."]


def _command_targets(name: str, args: list[str]) -> list[str]:
    if name in WRITES_EVERY_ARG:
        return _plain_args(args)
    if name in WRITES_DESTINATION:
        return _destinations(args)[1]
    if name in MOVES:  # 源被移走，目的地被写
        sources, written = _destinations(args)
        return [*sources, *written]
    if name in IN_PLACE_EDITORS and _is_in_place(args):
        return _plain_args(args)
    if name == "git":
        return _git_targets(args)
    if name == "dd":
        return [a[3:] for a in args if a.startswith("of=")]
    if name == "find":
        return _find_targets(args)
    return []


def _next_dir(current: str | None, args: list[str]) -> str | None:
    """`cd <目录>` 之后相对路径的前缀；看不出去哪了就返回 None。"""
    plain = _plain_args(args)
    if not plain or current is None:
        return None
    target = plain[0]
    if target.startswith(("~", "-")) or "$" in target or "`" in target:
        return None
    return target if os.path.isabs(target) else os.path.normpath(os.path.join(current, target))


def _under(directory: str | None, target: str) -> str:
    if not directory or directory == "." or os.path.isabs(target) or target.startswith(("~", "$")):
        return target
    return os.path.join(directory, target)


def _nested_script(name: str, args: list[str]) -> str | None:
    """`bash -c '<脚本>'` 与 `eval <脚本>` 里要再看一层的脚本文本。"""
    if name == "eval":
        return " ".join(args)
    if name in SHELLS:
        for index, arg in enumerate(args[:-1]):
            if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
                return args[index + 1]
    return None


def _simple_command(words: list[str], dirs: list[str | None], depth: int) -> list[str]:
    redirects, rest = _redirect_targets(words)
    rest = _strip_prefix(rest)
    targets = list(redirects)
    if rest:
        name, args = os.path.basename(rest[0]), rest[1:]
        script = _nested_script(name, args)
        if name in CHANGE_DIR:
            dirs[-1] = _next_dir(dirs[-1], args)
        elif script is not None:
            targets += _scan(script, depth + 1)
        else:
            targets += _command_targets(name, args)
    return [_under(dirs[-1], target) for target in targets if target]


def _narrow_glob(target: str) -> str:
    """`tests/*.py` 这类通配写法按它所在的目录算。"""
    match = GLOB_CHARS.search(target)
    if not match:
        return target
    return os.path.dirname(target[: match.start()]) or "."


def _scan(command: str, depth: int) -> list[str]:
    if depth > MAX_DEPTH:
        raise Unanalyzable(f"shell 嵌套超过 {MAX_DEPTH} 层")
    found: list[str] = []
    dirs: list[str | None] = [""]  # 当前目录相对会话 cwd 的前缀；括号开子 shell 时压栈
    for tokens in _logical_lines(command):
        words: list[str] = []
        for token in [*tokens, ";"]:
            if token in SEPARATORS or token in OPEN_GROUP or token in CLOSE_GROUP:
                found += _simple_command(words, dirs, depth)
                words = []
                if token in OPEN_GROUP:
                    dirs.append(dirs[-1])
                elif token in CLOSE_GROUP and len(dirs) > 1:
                    dirs.pop()
            else:
                words.append(token)
    return found


def write_targets(command: str) -> list[str]:
    """命令里看得出的写入目标（相对会话 cwd 的路径串，未解析）。"""
    if len(command) > MAX_COMMAND_CHARS:
        raise Unanalyzable(f"命令超过 {MAX_COMMAND_CHARS} 个字符")
    return [_narrow_glob(target) for target in _scan(command, 0)]
