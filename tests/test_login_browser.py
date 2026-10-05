"""RIUS-1227: /rius:login opens a browser only where a window can appear,
and never hands the terminal to a console browser such as lynx or w3m."""
import pathlib
import sys
import webbrowser

import pytest

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import rius_ctl  # noqa: E402
from rius_cc import login  # noqa: E402
from tests.test_login import LINK_RESPONSE, FakeServer  # noqa: E402

URL = "https://connect.example/pick"


class GuiBrowser(webbrowser.BaseBrowser):
    def __init__(self):
        super().__init__("gui")
        self.opened = []

    def open(self, url, new=0, autoraise=True):
        self.opened.append(url)
        return True


def _console_browser():
    browser = webbrowser.GenericBrowser("lynx")
    browser.opened = []
    browser.open = lambda url, new=0, autoraise=True: browser.opened.append(url)
    return browser


@pytest.fixture
def default_browser(monkeypatch):
    def install(browser):
        monkeypatch.setattr(rius_ctl.webbrowser, "get", lambda: browser)
        return browser
    return install


def _opened(browser, environ, system):
    rius_ctl._open_browser(URL, environ=environ, system=system)
    return browser.opened == [URL]


@pytest.mark.parametrize("environ, system", [
    ({}, "darwin"),
    ({}, "win32"),
    ({"DISPLAY": ":0"}, "linux"),
    ({"WAYLAND_DISPLAY": "wayland-0"}, "linux"),
])
def test_a_desktop_session_opens_the_browser(default_browser, environ, system):
    assert _opened(default_browser(GuiBrowser()), environ, system)


@pytest.mark.parametrize("environ, system", [
    ({}, "linux"),
    ({"TERM": "xterm"}, "linux"),
    ({"DISPLAY": ":0", "SSH_CONNECTION": "10.0.0.1 5022 10.0.0.2 22"}, "linux"),
    ({"WAYLAND_DISPLAY": "wayland-0", "SSH_TTY": "/dev/pts/3"}, "linux"),
    ({"SSH_CONNECTION": "10.0.0.1 5022 10.0.0.2 22"}, "darwin"),
    ({"SSH_TTY": "/dev/ttys004"}, "darwin"),
    ({"SSH_CONNECTION": "10.0.0.1 5022 10.0.0.2 22"}, "win32"),
])
def test_ssh_or_no_display_never_looks_for_a_browser(monkeypatch, environ, system):
    lookups = []
    monkeypatch.setattr(rius_ctl.webbrowser, "get",
                        lambda: lookups.append("get") or GuiBrowser())
    rius_ctl._open_browser(URL, environ=environ, system=system)
    assert lookups == []


def test_a_console_browser_is_never_launched(default_browser):
    browser = default_browser(_console_browser())
    assert not _opened(browser, {"DISPLAY": ":0"}, "linux")
    assert browser.opened == []


def test_a_background_launcher_such_as_xdg_open_is_used(default_browser, monkeypatch):
    browser = webbrowser.BackgroundBrowser("xdg-open")
    calls = []
    monkeypatch.setattr(browser, "open", lambda url, new=0, autoraise=True:
                        calls.append(url))
    default_browser(browser)
    rius_ctl._open_browser(URL, environ={"DISPLAY": ":0"}, system="linux")
    assert calls == [URL]


def test_no_browser_at_all_is_not_an_error(monkeypatch):
    def none_found():
        raise webbrowser.Error("could not locate runnable browser")
    monkeypatch.setattr(rius_ctl.webbrowser, "get", none_found)
    rius_ctl._open_browser(URL, environ={"DISPLAY": ":0"}, system="linux")


@pytest.mark.parametrize("agent_flag", [[], ["--agent", "codex"],
                                        ["--agent", "cursor"]])
def test_login_over_ssh_prints_the_link_and_the_code(tmp_path, monkeypatch,
                                                     capsys, agent_flag):
    monkeypatch.setattr(login, "_send", FakeServer((201, LINK_RESPONSE)))
    monkeypatch.setattr(rius_ctl.webbrowser, "get", lambda: pytest.fail(
        "looked for a browser over SSH"))
    for name in ("RIUS_API_KEY", "RIUS_ENV", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 5022 10.0.0.2 22")
    rius_ctl.dispatch(["login", "--cwd", "/opt/proj"] + agent_flag,
                      str(tmp_path))
    out = capsys.readouterr().out
    assert "Open:  " + LINK_RESPONSE["connect_url"] in out
    assert "Code:  " + LINK_RESPONSE["user_code"] in out
