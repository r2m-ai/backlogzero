import pytest
from sandboxing import SKIP_REASON, inside_sandbox

from agentops import token_setup

FAKE = "sk-ant-oat01-" + "Ab9_-" * 20  # 113 字符


def test_extract_token_ignores_ansi_and_wrapping_noise():
    transcript = (
        f"\x1b[32m✓ created\x1b[0m\n\n Your OAuth token:\n\n {FAKE}\n\n Store it securely\n"
    )

    assert token_setup.extract_token(transcript) == FAKE
    assert token_setup.extract_token("nothing here") is None


def test_mask_hides_token_body():
    masked = token_setup.mask(f"token: {FAKE} end")

    assert FAKE not in masked
    assert masked.startswith("token: sk-ant-oat01-••••") and masked.endswith(" end")


def test_run_in_pty_captures_output_at_requested_width(capsys):
    rc, transcript = token_setup.run_in_pty(
        ["sh", "-c", f"stty size; printf 'tok %s\\n' {FAKE}"], columns=250, forward_stdin=False
    )

    assert rc == 0
    assert "50 250" in transcript
    assert token_setup.extract_token(transcript) == FAKE
    assert FAKE not in capsys.readouterr().out  # 回显已打码


def test_run_in_pty_returns_child_exit_code():
    rc, _ = token_setup.run_in_pty(["sh", "-c", "exit 4"], forward_stdin=False)

    assert rc == 4


def test_setup_refuses_when_no_token_in_output(monkeypatch):
    monkeypatch.setattr(token_setup, "SETUP_COMMAND", ("sh", "-c", "echo no token here"))
    stored = []
    monkeypatch.setattr(token_setup, "store_token", lambda token: stored.append(token))

    with pytest.raises(token_setup.AuthError):
        token_setup.setup()
    assert stored == []


def test_setup_stores_and_reads_back(monkeypatch):
    monkeypatch.setattr(token_setup, "SETUP_COMMAND", ("sh", "-c", f"echo 'Your token: {FAKE}'"))
    stored = []
    monkeypatch.setattr(token_setup, "store_token", lambda token: stored.append(token))
    monkeypatch.setattr(token_setup.auth, "read_token", lambda: FAKE)

    message = token_setup.setup()

    assert stored == [FAKE]
    assert "113 字符" in message and FAKE not in message


def test_feed_pty_types_lines_into_a_tty_reader():
    """read -s 从 TTY 读，不读 stdin；伪终端喂入必须能到达它。"""
    rc = token_setup.feed_pty(
        ["sh", "-c", 'read -s a; read -s b; [ "$a" = one ] && [ "$b" = two ]'], ["one", "two"]
    )

    assert rc == 0


@pytest.mark.skipif(inside_sandbox(), reason=SKIP_REASON)
def test_store_token_round_trips_through_the_real_keychain():
    import subprocess

    service = f"agentops-test-{__import__('os').getpid()}"
    try:
        token_setup.store_token(FAKE, service=service)
        read_back = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert read_back == FAKE
    finally:
        subprocess.run(
            ["/usr/bin/security", "delete-generic-password", "-s", service], capture_output=True
        )
