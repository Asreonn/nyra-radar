from radar.extraction.canonical_url import canonicalize, github_repo_from_url


def test_canonical_removes_tracking_and_normalizes_hosts():
    assert canonicalize("https://m.reddit.com/r/linux/comments/abc/title/?utm_source=x#frag") == \
        "https://www.reddit.com/r/linux/comments/abc/"
    assert canonicalize("https://example.com/story/?utm_campaign=x&b=2&a=1") == \
        "https://example.com/story?a=1&b=2"


def test_github_repo_detection():
    assert github_repo_from_url("https://github.com/OpenAI/codex/issues/12") == "OpenAI/codex"
    assert github_repo_from_url("https://github.com/features/copilot") is None
