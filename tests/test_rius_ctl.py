import json
import subprocess
import sys
import pathlib

CTL = str(pathlib.Path(__file__).parent.parent / "scripts" / "rius_ctl.py")


def _run(args, home, env=None):
    e = {"HOME": home, "PATH": "/usr/bin:/bin"}
    e.update(env or {})
    return subprocess.run([sys.executable, CTL] + args,
                          capture_output=True, text=True, env=e, timeout=30)


def test_status_explains_why_it_is_off(tmp_path):
    home = str(tmp_path)
    (tmp_path / ".claude" / "rius").mkdir(parents=True)
    r = _run(["status", "--session", "s1", "--cwd", "/x/y"], home,
             {"RIUS_API_KEY": "glassflow_k"})
    assert r.returncode == 0
    assert "off" in r.stdout.lower()
    assert "/x/y" in r.stdout or "default" in r.stdout.lower()


def test_status_never_prints_the_key(tmp_path):
    home = str(tmp_path)
    (tmp_path / ".claude" / "rius").mkdir(parents=True)
    r = _run(["status", "--session", "s1", "--cwd", "/x"], home,
             {"RIUS_API_KEY": "glassflow_supersecret"})
    assert "supersecret" not in r.stdout


def test_on_then_status_reports_on(tmp_path):
    home = str(tmp_path)
    (tmp_path / ".claude" / "rius").mkdir(parents=True)
    _run(["on", "--session", "s1", "--cwd", "/x"], home, {"RIUS_API_KEY": "glassflow_k"})
    r = _run(["status", "--session", "s1", "--cwd", "/x"], home, {"RIUS_API_KEY": "glassflow_k"})
    assert "on" in r.stdout.lower()


def test_enable_here_adds_a_path_rule(tmp_path):
    home = str(tmp_path)
    (tmp_path / ".claude" / "rius").mkdir(parents=True)
    _run(["enable-here", "--session", "s1", "--cwd", "/opt/proj"], home,
         {"RIUS_API_KEY": "glassflow_k"})
    rules = json.load(open(str(tmp_path / ".claude" / "rius" / "config.json")))
    assert "/opt/proj" in rules["enabled_paths"]


def test_enable_here_is_idempotent(tmp_path):
    home = str(tmp_path)
    (tmp_path / ".claude" / "rius").mkdir(parents=True)
    for _ in range(3):
        _run(["enable-here", "--session", "s1", "--cwd", "/opt/proj"], home,
             {"RIUS_API_KEY": "glassflow_k"})
    rules = json.load(open(str(tmp_path / ".claude" / "rius" / "config.json")))
    assert rules["enabled_paths"].count("/opt/proj") == 1


def test_unknown_action_exits_zero_with_usage(tmp_path):
    home = str(tmp_path)
    (tmp_path / ".claude" / "rius").mkdir(parents=True)
    r = _run(["wat", "--session", "s1", "--cwd", "/x"], home)
    assert r.returncode == 0
    assert "usage" in r.stdout.lower()
