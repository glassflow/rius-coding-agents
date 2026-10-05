"""RIUS-1221: what each content mode puts on the wire, read from the OTLP
bytes the exporter hands to the network.

The planted secrets are assembled at runtime so the repo's own secret scan
never sees a key-shaped literal.
"""
import json

import pytest

import exporter
from rius_cc import config, scrub
from tests.signed_in import sign_in

SID = "77777777-7777-7777-7777-777777777777"
CWD = "/tmp/proj"

SECRETS = {
    "prompt": "AKIA" + "IOSFODNN7EXAMPLE",
    "prompt_pair": "hunter2" + "hunter2",
    "title": "ghp_" + "T" * 36,
    "tool_input": "tok" * 8 + "BEARER",
    "tool_output": "ghp_" + "A" * 36,
    "pem": "MIIEsecret" + "PEMbody",
    "assistant": "sk-ant-" + "api03-" + "x" * 30,
    "jwt": "eyJ" + "hbGciOiJIUzI1" + ".eyJ" + "zdWIiOiIxMjM0" + ".sig_abcdef",
    "subagent_description": "s3cr3t" + "value99",
    "subagent_prompt": "xoxb-" + "1234567890-abcdefghij",
    "subagent_output": "sk_live_" + "Z" * 24,
    "env_file": "PLAIN_VALUE_FROM_" + "ENV_FILE",
    "tool_error": "AIza" + "B" * 35,
}
# Content with no secret in it, which structure only must withhold too.
PLAIN = ("please refactor the parser", "the parser now streams",
         "Explore the parser module")


def _line(kind, uuid, parent, ts, message, **extra):
    entry = {"sessionId": SID, "cwd": CWD, "gitBranch": "main",
             "version": "2.1.278", "parentUuid": parent, "isSidechain": False,
             "type": kind, "uuid": uuid,
             "timestamp": "2026-09-22T10:00:%02d.000Z" % ts, "message": message}
    entry.update(extra)
    return json.dumps(entry)


def _user(uuid, parent, ts, content, **extra):
    return _line("user", uuid, parent, ts, {"role": "user", "content": content},
                 promptId="p1", **extra)


def _assistant(uuid, parent, ts, content, stop="tool_use", **extra):
    return _line("assistant", uuid, parent, ts, {
        "role": "assistant", "model": "claude-opus-5", "stop_reason": stop,
        "content": content, "usage": {"input_tokens": 10, "output_tokens": 5}},
        **extra)


def _tool_use(tool_id, name, tool_input):
    return [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]


def _result(tool_id, content, is_error=False):
    return [{"type": "tool_result", "tool_use_id": tool_id,
             "is_error": is_error, "content": content}]


def _main_transcript():
    s = SECRETS
    prompt = "%s: deploy with %s and DB_PASSWORD=%s" % (PLAIN[0], s["prompt"],
                                                        s["prompt_pair"])
    return [
        json.dumps({"type": "custom-title", "customTitle": "fix " + s["title"],
                    "sessionId": SID}),
        _user("u1", None, 0, [{"type": "text", "text": prompt}]),
        _assistant("a1", "u1", 1, _tool_use("t_bash", "Bash", {
            "command": "curl -H 'Authorization: Bearer %s' x" % s["tool_input"]})),
        _user("u2", "a1", 2, _result("t_bash", "%s\n-----BEGIN RSA PRIVATE KEY-----"
                                     "\n%s\n-----END RSA PRIVATE KEY-----"
                                     % (s["tool_output"], s["pem"])),
              sourceToolAssistantUUID="a1"),
        _assistant("a2", "u2", 3, _tool_use("t_env", "Read",
                                            {"file_path": CWD + "/.env"})),
        _user("u3", "a2", 4, _result("t_env", "DB_URL=" + s["env_file"]),
              sourceToolAssistantUUID="a2"),
        _assistant("a3", "u3", 5, _tool_use("t_fail", "Bash", {"command": "make"})),
        _user("u4", "a3", 6, _result("t_fail", "Exit code 1\nerror: bad key %s"
                                     % s["tool_error"], is_error=True),
              sourceToolAssistantUUID="a3"),
        _assistant("a4", "u4", 7, _tool_use("t_agent", "Agent", {
            "subagent_type": "general-purpose",
            "prompt": "%s using %s" % (PLAIN[2], s["subagent_prompt"])})),
        _user("u5", "a4", 12, _result("t_agent", "explored"),
              sourceToolAssistantUUID="a4"),
        _assistant("a5", "u5", 13, [{"type": "text", "text": "%s; key %s jwt %s"
                                     % (PLAIN[1], s["assistant"], s["jwt"])}],
                   stop="end_turn"),
    ]


def _subagent_transcript():
    s = SECRETS
    extra = {"isSidechain": True, "agentId": "sub1"}
    first = _line("user", "sa0", None, 8, {"role": "user", "content": "%s using %s"
                                           % (PLAIN[2], s["subagent_prompt"])},
                  promptId="p1", **extra)
    return [
        first,
        _assistant("sa1", "sa0", 9, _tool_use("t_sub", "Read",
                                              {"file_path": CWD + "/a.py"}), **extra),
        _line("user", "sa2", "sa1", 10, {"role": "user", "content": _result(
            "t_sub", "stripe = %s" % s["subagent_output"])}, **extra),
        _assistant("sa3", "sa2", 11, [{"type": "text", "text": "done"}],
                   stop="end_turn", **extra),
    ]


def _write_session(tmp_path):
    path = tmp_path / (SID + ".jsonl")
    path.write_text("\n".join(_main_transcript()) + "\n")
    sub_dir = tmp_path / SID / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "agent-sub1.jsonl").write_text("\n".join(_subagent_transcript()) + "\n")
    (sub_dir / "agent-sub1.meta.json").write_text(json.dumps({
        "agentType": "general-purpose", "toolUseId": "t_agent", "spawnDepth": 1,
        "description": "%s token=%s" % (PLAIN[2], SECRETS["subagent_description"]),
        "model": "haiku"}))
    return path


@pytest.fixture
def wire(monkeypatch):
    """Every OTLP body handed to the network, as the real encoder made it."""
    bodies = []
    monkeypatch.setattr(exporter.otlp, "export",
                        lambda ep, key, body, timeout=5.0: bodies.append(body) or 200)
    return bodies


def _export(tmp_path, wire, with_content):
    home = tmp_path / "home"
    (home / ".claude" / "rius").mkdir(parents=True)
    sign_in(home)
    config.write_path_rules(str(home), {
        "enabled_paths": ["/tmp"],
        config.CONTENT_CHOICES_KEY: {"/tmp": with_content}})
    path = _write_session(tmp_path)
    for event in ("SessionStart", "PostToolUse", "Stop", "SessionEnd"):
        exporter.run(event, {"session_id": SID, "transcript_path": str(path),
                             "cwd": CWD, "hook_event_name": event}, {}, str(home))
    assert wire, "nothing was exported"
    return b"".join(wire)


def test_with_content_no_planted_secret_reaches_the_wire(tmp_path, wire):
    sent = _export(tmp_path, wire, with_content=True)
    leaked = [field for field, secret in SECRETS.items()
              if secret.encode() in sent]
    assert leaked == []


def test_with_content_the_content_itself_still_arrives(tmp_path, wire):
    sent = _export(tmp_path, wire, with_content=True)
    for plain in PLAIN:
        assert plain.encode() in sent, plain
    for name in ("aws-key", "password", "authorization", "github-token",
                 "private-key", "anthropic-key", "jwt", "slack-token",
                 "stripe-key", "gcp-key", "secret-file", "token"):
        assert scrub.marker(name).encode() in sent, name
    assert b"fix [redacted:github-token]" in sent                # the title
    assert b"error: bad key [redacted:gcp-key]" in sent          # status line
    assert b"stripe = [redacted:stripe-key]" in sent             # subagent tool


def test_structure_only_sends_no_content_at_all(tmp_path, wire):
    sent = _export(tmp_path, wire, with_content=False)
    for text in list(SECRETS.values()) + list(PLAIN):
        assert text.encode() not in sent, text
    assert b"[redacted:" not in sent
    assert b"general-purpose" in sent            # identity still arrives
    assert b"claude-opus-5" in sent
