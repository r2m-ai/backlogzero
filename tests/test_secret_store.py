from agentops import secret_store
from agentops.auth import AuthError


def test_value_problem_rejects_empty_whitespace_and_short():
    assert secret_store.value_problem("") == "为空"
    assert "空白" in secret_store.value_problem("abc def0123456789ab")
    assert "不像" in secret_store.value_problem("short")
    assert secret_store.value_problem("OeAFFmMliFG5orCUuwAKQ8l4WWFQ67YX") is None


def test_service_name_validates_name():
    assert secret_store.service_name("eodhd") == "agentops-eodhd"
    try:
        secret_store.service_name("Bad Name")
    except AuthError:
        pass
    else:
        raise AssertionError("expected AuthError")


def test_set_secret_stores_and_reads_back(monkeypatch):
    # Arrange：假的 security 后端
    store = {}
    monkeypatch.setattr(
        secret_store.token_setup, "store_token", lambda v, *, service: store.__setitem__(service, v)
    )
    monkeypatch.setattr(secret_store, "read_secret", lambda name: store["agentops-" + name])

    # Act
    message = secret_store.set_secret("eodhd", value="  OeAFFmMliFG5orCUuwAKQ8l4WWFQ67YX\n")

    # Assert：去掉首尾空白后存入；结论里不含值
    assert store["agentops-eodhd"] == "OeAFFmMliFG5orCUuwAKQ8l4WWFQ67YX"
    assert "32 字符" in message and "OeAFF" not in message
