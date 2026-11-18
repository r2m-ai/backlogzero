from agentops import review


def test_default_reviews_root_is_under_agentops_home(agentops_home, monkeypatch):
    monkeypatch.delenv(review.ENV_REVIEWS_DIR, raising=False)

    assert review.reviews_root() == agentops_home / "reviews"


def test_default_review_paths_keep_inputs_under_state_dir(agentops_home, monkeypatch):
    monkeypatch.delenv(review.ENV_REVIEWS_DIR, raising=False)

    paths = review.review_paths(3)

    for path in paths.values():
        assert agentops_home / "reviews" in path.parents


def test_override_env_still_wins(agentops_home, monkeypatch, tmp_path):
    override = tmp_path / "elsewhere"
    monkeypatch.setenv(review.ENV_REVIEWS_DIR, str(override))

    assert review.reviews_root() == override
