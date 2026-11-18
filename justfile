set shell := ["bash", "-euo", "pipefail", "-c"]
export PYTHONHASHSEED := "0"

default: check

# 安装依赖到 .venv
sync:
    uv sync

# 运行测试
test *ARGS:
    uv run --offline pytest {{ARGS}}

lint:
    uv run --offline ruff check src tests hooks

format:
    uv run --offline ruff format src tests hooks

# lint + test
check: lint test

# 把 ao 装到 ~/.local/bin，供 hooks、launchd、会话调用。只在会话外由人执行。
install:
    uv tool install --editable . --force --reinstall

# ---- 流水线要求的 recipe（本仓库自己也走 agentops 流水线）----

# 快速自检（会话里常用）
test-fast: lint
    uv run --offline pytest -q -x

# 完整测试，合并队列只认这个；不联网
test-all: check

# 定时采集：套件没有数据采集任务
capture:
    @echo "agentops: 没有采集任务"

# 夜间任务：套件没有夜间任务
nightly:
    @echo "agentops: 没有夜间任务"
