"""rius_ctl install-hooks --agent cursor: merge into a hooks.json, never
replace it. Temp files only."""
import json
import os

import rius_ctl
from rius_cc import cursor_install
from tests.platforms import posix_only

ROOT = cursor_install.plugin_root()
EVENTS = set(json.load(open(os.path.join(ROOT, "cursor", "hooks.json")))["hooks"])
THEIRS = {"command": "./audit.sh", "timeout": 5}


def _write(path, doc):
    path.write_text(json.dumps(doc))


def _read(path):
    return json.loads(path.read_text())


def _rius(doc):
    return [e for entries in doc["hooks"].values() for e in entries
            if cursor_install.is_rius_entry(e)]


def test_a_missing_file_gets_every_event(tmp_path):
    path = tmp_path / "hooks.json"
    changed, count = cursor_install.install(str(path), ROOT)
    doc = _read(path)
    assert changed and count == len(EVENTS)
    assert set(doc["hooks"]) == EVENTS and doc["version"] == 1


def test_commands_name_the_real_plugin_root(tmp_path):
    path = tmp_path / "hooks.json"
    cursor_install.install(str(path), ROOT)
    for entry in _rius(_read(path)):
        assert "${" not in entry["command"]
        assert '"%s/scripts/hook.sh"' % ROOT in entry["command"]


def test_other_hooks_are_kept(tmp_path):
    path = tmp_path / "hooks.json"
    _write(path, {"version": 1, "hooks": {"stop": [THEIRS],
                                          "workspaceOpen": [THEIRS]}})
    cursor_install.install(str(path), ROOT)
    doc = _read(path)
    assert doc["hooks"]["stop"][0] == THEIRS
    assert doc["hooks"]["workspaceOpen"] == [THEIRS]


def test_installing_twice_changes_nothing(tmp_path):
    path = tmp_path / "hooks.json"
    _write(path, {"version": 1, "hooks": {"stop": [THEIRS]}})
    cursor_install.install(str(path), ROOT)
    first = path.read_text()
    changed, _ = cursor_install.install(str(path), ROOT)
    assert not changed and path.read_text() == first


def test_an_upgrade_replaces_the_stale_entries(tmp_path):
    path = tmp_path / "hooks.json"
    old = {event: [dict(e, command=e["command"].replace(ROOT, "/old/plugin"))
                   for e in entries]
           for event, entries in cursor_install.shipped_hooks(ROOT).items()}
    _write(path, cursor_install.merge({"hooks": {}}, old))
    cursor_install.install(str(path), ROOT)
    entries = _rius(_read(path))
    assert len(entries) == len(EVENTS)
    assert not any("/old/plugin" in e["command"] for e in entries)


def test_uninstall_takes_out_only_rius(tmp_path):
    path = tmp_path / "hooks.json"
    _write(path, {"version": 1, "hooks": {"stop": [THEIRS]}})
    cursor_install.install(str(path), ROOT)
    changed, count = cursor_install.uninstall(str(path))
    assert changed and count == 0
    assert _read(path) == {"version": 1, "hooks": {"stop": [THEIRS]}}


def test_invalid_json_is_left_alone(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text("{not json")
    try:
        cursor_install.install(str(path), ROOT)
    except cursor_install.HooksFileError:
        pass
    else:
        raise AssertionError("invalid JSON was overwritten")
    assert path.read_text() == "{not json"


@posix_only("Windows has no file permission bits")
def test_the_file_mode_is_kept(tmp_path):
    path = tmp_path / "hooks.json"
    _write(path, {"hooks": {}})
    os.chmod(path, 0o600)
    cursor_install.install(str(path), ROOT)
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_the_cli_installs_into_the_given_path(tmp_path, capsys):
    path = tmp_path / "hooks.json"
    rius_ctl.dispatch(["install-hooks", "--agent", "cursor", "--path",
                       str(path)], str(tmp_path / "home"))
    assert "Wrote %d Rius hooks" % len(EVENTS) in capsys.readouterr().out
    assert len(_rius(_read(path))) == len(EVENTS)


def test_the_cli_defaults_to_the_cursor_user_hooks(tmp_path, capsys):
    home = tmp_path / "home"
    rius_ctl.dispatch(["install-hooks", "--agent", "cursor"], str(home))
    assert (home / ".cursor" / "hooks.json").exists()


def test_the_cli_refuses_without_the_cursor_agent(tmp_path, capsys):
    rius_ctl.dispatch(["install-hooks", "--path", str(tmp_path / "h.json")],
                      str(tmp_path))
    assert "Cursor only" in capsys.readouterr().out
    assert not (tmp_path / "h.json").exists()


def test_reinstalling_keeps_hooks_added_after_ours_in_place(tmp_path):
    path = tmp_path / "hooks.json"
    cursor_install.install(str(path), ROOT)
    doc = _read(path)
    doc["hooks"]["stop"].append(THEIRS)
    _write(path, doc)
    changed, _ = cursor_install.install(str(path), ROOT)
    assert not changed and _read(path)["hooks"]["stop"][-1] == THEIRS
