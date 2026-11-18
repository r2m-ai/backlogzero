from agentops import doctor


def test_version_at_least():
    assert doctor.version_at_least("2.1.271", "2.1.259")
    assert doctor.version_at_least("2.1.259", "2.1.259")
    assert not doctor.version_at_least("2.1.100", "2.1.259")
    assert not doctor.version_at_least("garbage", "2.1.259")


def test_run_checks_without_auth_covers_tools_and_state(agentops_home):
    checks = doctor.run_checks(include_auth=False)

    names = {check.name for check in checks}
    assert {"claude", "just", "uv", "duckdb", "state dir", "config"} <= names
    assert all(check.status in {"OK", "WARN", "FAIL"} for check in checks)


def test_exit_code_fails_only_on_fail():
    ok = doctor.Check("a", doctor.OK, "")
    warn = doctor.Check("b", doctor.WARN, "")
    fail = doctor.Check("c", doctor.FAIL, "")

    assert doctor.exit_code((ok, warn)) == 0
    assert doctor.exit_code((ok, fail)) == 1


def test_token_shape_problem_catches_wrong_values():
    from agentops import auth

    assert auth.token_shape_problem("x" * 256) is not None
    assert auth.token_shape_problem("sk-ant-oat01-short") is not None
    assert auth.token_shape_problem("sk-ant-oat01-" + "a" * 95 + "\n") is not None
    assert auth.token_shape_problem("sk-ant-oat01-" + "a" * 36) is not None  # 49 字符，折行截断
    assert auth.token_shape_problem("sk-ant-oat01-" + "a" * 95) is None
