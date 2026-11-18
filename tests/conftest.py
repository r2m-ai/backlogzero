import pytest

from agentops import init


@pytest.fixture
def agentops_home(tmp_path, monkeypatch):
    """把运行状态指到临时目录并初始化，每个测试互不影响。"""
    home = tmp_path / "agentops-state"
    monkeypatch.setenv("AGENTOPS_HOME", str(home))
    # 评审记录也指到临时目录：测试绝不能碰 docs/reviews 里的真实记录
    monkeypatch.setenv("AGENTOPS_REVIEWS_DIR", str(tmp_path / "reviews"))
    init.initialize()
    return home
