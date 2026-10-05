"""The secret scrubber, one format at a time. Key-shaped literals are built
at runtime so the repo's secret scan never sees one."""
import time

import pytest

from rius_cc import scrub, spans

CASES = [
    ("aws-key", "AKIA" + "IOSFODNN7EXAMPLE"),
    ("aws-key", "ASIA" + "Q" * 16),
    ("gcp-key", "AIza" + "S" * 35),
    ("gcp-token", "ya29." + "t" * 30),
    ("github-token", "ghp_" + "g" * 36),
    ("github-token", "github_pat_" + "p" * 30),
    ("slack-token", "xoxb-" + "123456789012-abc"),
    ("slack-webhook", "https://hooks.slack.com/services/" + "T000/B000/" + "x" * 20),
    ("stripe-key", "sk_live_" + "s" * 24),
    ("stripe-key", "whsec_" + "w" * 24),
    ("anthropic-key", "sk-ant-" + "api03-" + "a" * 30),
    ("openai-key", "sk-proj-" + "o" * 30),
    ("openai-key", "sk-" + "o" * 40),
    ("rius-key", "ri_" + "abcd1234" + "." + "efgh5678ij"),
    ("jwt", "eyJ" + "hbGciOiJIUzI1" + ".eyJ" + "zdWIiOiIxMjM0" + ".c2lnbmF0dXJl"),
    ("private-key", "-----BEGIN OPENSSH PRIVATE KEY-----\nb3Blbn\n"
                    "-----END OPENSSH PRIVATE KEY-----"),
    ("authorization", "Authorization: Bearer " + "abc.def-123456789"),
    ("authorization", "authorization: Basic " + "dXNlcjpwYXNzd29yZA=="),
    ("bearer", "Bearer " + "b" * 24),
]


@pytest.mark.parametrize("name, secret", CASES)
def test_each_format_becomes_its_marker(name, secret):
    out = scrub.scrub("before %s after" % secret)
    assert out == "before %s after" % scrub.marker(name)


@pytest.mark.parametrize("text, name", [
    ("DB_PASSWORD=hunter2", "password"),
    ("--password=hunter2", "password"),
    ("client_secret: abc123", "secret"),
    ('"api_key": "abc 123"', "api-key"),
    ("GITHUB_TOKEN=abc", "token"),
    ("AWS_SECRET_ACCESS_KEY=abc", "access-key"),
    ("?token=abc&x=1", "token"),
    ('{"command": "PASSWORD=\\"a b\\" run"}', "password"),
])
def test_a_secret_pair_keeps_its_key_and_loses_its_value(text, name):
    out = scrub.scrub(text)
    assert scrub.marker(name) in out
    for value in ("hunter2", "abc", "a b"):
        assert value not in out.replace(scrub.marker(name), "")


def test_an_unterminated_private_key_is_removed_to_the_end():
    out = scrub.scrub("x\n-----BEGIN RSA PRIVATE KEY-----\nMIIabc\ncut here")
    assert out == "x\n" + scrub.marker("private-key")


@pytest.mark.parametrize("text", [
    "input_tokens: 12, max_tokens=4096",
    "tokenizer=fast; secretary: Jane",
    "a basic understanding, the bearer of news",
    "if token == expected:",
    "sk-short and AKIA123",
])
def test_ordinary_text_is_left_alone(text):
    assert scrub.scrub(text) == text


ADVERSARIAL = {
    "one word": "a" * 32768,
    "dotted": "a." * 16384,
    "secret words": "token" * 6553,
    "pairs": "token=" * 5461,
    "open keys": "-----BEGIN PRIVATE KEY-----" * 1213,
    "jwt heads": "eyJ" * 10922,
    "bearers": "Bearer " * 4681,
    "quotes": 'password: "' * 2978,
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_a_32kb_value_scrubs_in_linear_time(name):
    started = time.perf_counter()
    scrub.scrub(ADVERSARIAL[name])
    assert time.perf_counter() - started < 0.5, name


def test_a_secret_cut_by_the_cap_is_still_removed():
    secret = "ghp_" + "c" * 36
    value = "x" * 100 + " " + secret + " " + "y" * 1000
    out = spans.exportable(value, limit=110)
    assert "ghp_" not in out
    assert "truncated" in out


def test_exportable_keeps_a_short_value_whole():
    assert spans.exportable("hello", 100) == "hello"


@pytest.mark.parametrize("path", [
    ".env", "/p/.env.local", "C:\\keys\\server.pem", "/home/me/.ssh/id_rsa",
    "id_rsa.pub", "/x/credentials.json", "tls.KEY", "~/.npmrc", ".pypirc",
    "/root/.netrc",
])
def test_secret_shaped_paths(path):
    assert scrub.is_secret_path(path)


@pytest.mark.parametrize("path", ["main.py", "environment.md", "/p/keys/README",
                                  "src/env.ts"])
def test_ordinary_paths(path):
    assert not scrub.is_secret_path(path)


@pytest.mark.parametrize("tool_input, secret", [
    ('{"file_path": "/p/.env"}', True),
    ('{"path": "/p/certs/a.pem"}', True),
    ('{"command": "cat ./config/.env | head"}', True),
    ('{"command": "docker run --env-file=.env.prod app"}', True),
    ('{"command": "ls -la"}', False),
    ('{"file_path": "/p/main.py"}', False),
    ('not json', False),
])
def test_reads_secret_file(tool_input, secret):
    assert scrub.reads_secret_file(tool_input) is secret
