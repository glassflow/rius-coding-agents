"""`user.id` names the developer: the member who approved the key at
/rius:login (RIUS-1301).

Rius lists a coding session under its user only when a span carries
`user.id`; a resource-level one is ignored, and a session still running lists
by its pending spans. So every span carries it, pending ones too, on every
harness. A key stored with `use-key` was minted in the console and is often
shared, so it names nobody rather than whoever stored it. Nothing in the
environment can set it: the environment only turns things off.
"""
import pathlib
import shutil

import pytest

import exporter
from rius_cc import agent, config, cursor_events, cursor_export, login
from tests import cursor_fixtures, signed_in

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
EMAIL = "dev@example.com"
CC_SESSION = "66666666-6666-6666-6666-666666666666"
CC_EVENTS = ("SessionStart", "PostToolUse", "Stop", "SessionEnd")
CODEX_THREAD = "01a10b65-83da-7ab0-99f7-8e8364ecc4b0"
CURSOR_CID = "c0ffee00-0000-4000-8000-000000000001"
# What a developer might try: a variable named for it, and the OTel one.
HOSTILE_ENV = {"RIUS_USER_ID": "someone@else.example",
               "OTEL_RESOURCE_ATTRIBUTES": "user.id=someone@else.example"}


@pytest.fixture
def sent(monkeypatch):
    """(resource attributes, spans) per batch the exporter encodes."""
    batches = []
    monkeypatch.setattr(exporter.otlp, "encode",
                        lambda resource, out: batches.append(
                            (resource, list(out))) or b"x")
    monkeypatch.setattr(exporter.otlp, "export", lambda *a, **k: 200)
    return batches


def _use_key(home):
    """What `rius_ctl.sh use-key` writes: a key, an endpoint, no member."""
    login._write_private(login.credentials_path(home), {
        "api_key": signed_in.TEST_KEY, "endpoint": signed_in.TEST_ENDPOINT,
        "env": "production", "source": login.USE_KEY_SOURCE})


def _claude_code(home, env=None):
    config.write_path_rules(home, {"enabled_paths": ["/tmp"]})
    transcript = str(FIXTURES / "tool_errors_bash.jsonl")
    for event in CC_EVENTS:
        exporter.run(event, {"session_id": CC_SESSION, "cwd": "/tmp/proj",
                             "transcript_path": transcript,
                             "hook_event_name": event},
                     env or {}, home)


def _codex(home, env=None):
    folder = pathlib.Path(home) / "sessions"
    shutil.copytree(str(FIXTURES / "codex" / "subagent"), str(folder))
    rollout = next(folder.glob("rollout-*-%s.jsonl" % CODEX_THREAD))
    config.write_path_rules(home, {"enabled_paths": ["/tmp"]})
    for event in ("SessionStart", "Stop"):
        exporter.run(event, {"session_id": CODEX_THREAD, "cwd": "/tmp/proj",
                             "transcript_path": str(rollout),
                             "hook_event_name": event},
                     env or {}, home)


def _cursor(home, env=None):
    config.write_path_rules(home, {"enabled_paths": ["/w"]})
    sdir = cursor_export.spool_dir(home)
    tick = cursor_fixtures.clock()
    payloads = cursor_fixtures.payloads("docs_session")
    cursor_events.record(payloads[0], sdir, True, 32768, clock=tick)
    exporter.run_cursor("sessionStart",
                        {"conversation_id": CURSOR_CID, "cwd": "/w"},
                        env or {}, home)
    for payload in payloads[1:]:
        cursor_events.record(payload, sdir, True, 32768, clock=tick)
    exporter.run_cursor("stop", {"conversation_id": CURSOR_CID, "cwd": "/w"},
                        env or {}, home)


# Each agent keeps its own credential, so a test signs in under the profile.
HARNESSES = {"claude_code": (agent.CLAUDE_CODE, _claude_code),
             "codex": (agent.CODEX, _codex),
             "cursor": (agent.CURSOR, _cursor)}


@pytest.fixture(params=sorted(HARNESSES))
def harness(request):
    profile, run = HARNESSES[request.param]
    with agent.using(profile):
        yield run


def _spans(batches):
    return [s for _, out in batches for s in out]


def test_every_span_names_the_signed_in_member(harness, tmp_path, sent):
    signed_in.sign_in(str(tmp_path), email=EMAIL)
    harness(str(tmp_path))
    out = _spans(sent)
    assert out, "the exporter sent nothing"
    assert any(s.pending for s in out), "no pending span to check"
    assert {s.attributes.get("user.id") for s in out} == {EMAIL}


def test_user_id_is_never_a_resource_attribute(harness, tmp_path, sent):
    # Core ignores a resource-level user.id on purpose; one there would only
    # look like it worked.
    signed_in.sign_in(str(tmp_path), email=EMAIL)
    harness(str(tmp_path))
    assert sent and all("user.id" not in resource for resource, _ in sent)


def test_a_use_key_key_names_nobody(harness, tmp_path, sent):
    _use_key(str(tmp_path))
    harness(str(tmp_path))
    out = _spans(sent)
    assert out, "the exporter sent nothing"
    assert not any("user.id" in s.attributes for s in out)


def test_the_environment_cannot_name_a_user(tmp_path, sent):
    _use_key(str(tmp_path))
    _claude_code(str(tmp_path), env=HOSTILE_ENV)
    out = _spans(sent)
    assert out and not any("user.id" in s.attributes for s in out)
    assert all("user.id" not in resource for resource, _ in sent)


def test_the_environment_cannot_replace_the_member(tmp_path, sent):
    signed_in.sign_in(str(tmp_path), email=EMAIL)
    _claude_code(str(tmp_path), env=HOSTILE_ENV)
    assert {s.attributes.get("user.id") for s in _spans(sent)} == {EMAIL}


@pytest.mark.parametrize("email, expected", [
    (EMAIL, EMAIL),
    ("  " + EMAIL + "\n", EMAIL),
    ("", None),
    ("   ", None),
    (None, None),
    (42, None),
])
def test_resolve_reads_the_member_from_the_login_credential(tmp_path, email,
                                                           expected):
    signed_in.sign_in(str(tmp_path), email=email)
    assert config.resolve("s1", "/tmp", {}, str(tmp_path)).user_id == expected


def test_resolve_names_nobody_for_a_use_key_key(tmp_path):
    _use_key(str(tmp_path))
    c = config.resolve("s1", "/tmp", {}, str(tmp_path))
    assert c.key_source == login.USE_KEY_SOURCE and c.user_id is None


def test_resolve_names_nobody_when_signed_out(tmp_path):
    assert config.resolve("s1", "/tmp", {}, str(tmp_path)).user_id is None


def test_a_use_key_key_names_nobody_even_beside_an_email(tmp_path):
    # use-key writes a fresh credential today; the guard is the source, not
    # the missing field, so a merge that kept a login's email changes nothing.
    login._write_private(login.credentials_path(str(tmp_path)), {
        "api_key": signed_in.TEST_KEY, "endpoint": signed_in.TEST_ENDPOINT,
        "env": "production", "source": login.USE_KEY_SOURCE, "email": EMAIL})
    assert config.resolve("s1", "/tmp", {}, str(tmp_path)).user_id is None
