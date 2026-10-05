"""The Cursor plugin's files: it must load only its own hooks, commands and
MCP config, never Claude Code's, and every hook it wires must be one the
span builder understands."""
import json
import pathlib
import re

from rius_cc import cursor_spans, cursor_hook

ROOT = pathlib.Path(__file__).parent.parent
PLUGIN = json.loads((ROOT / ".cursor-plugin" / "plugin.json").read_text())
MARKET = json.loads((ROOT / ".cursor-plugin" / "marketplace.json").read_text())
HOOKS = json.loads((ROOT / "cursor" / "hooks.json").read_text())["hooks"]
COMMANDS = sorted((ROOT / "cursor" / "commands").glob("*.md"))
CC_PLUGIN = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())


def test_every_component_path_is_explicit_and_cursor_only():
    # Left out, Cursor falls back to hooks/hooks.json and commands/: the
    # Claude Code files.
    assert PLUGIN["hooks"] == "./cursor/hooks.json"
    assert PLUGIN["commands"] == "./cursor/commands"
    assert PLUGIN["mcpServers"] == "./cursor/mcp.json"
    for key in ("hooks", "commands", "mcpServers"):
        assert (ROOT / PLUGIN[key]).exists(), PLUGIN[key]


def test_no_default_discovery_folder_exists():
    for folder in ("skills", "rules", "agents"):
        assert not (ROOT / folder).exists(), folder
    assert not (ROOT / "mcp.json").exists()


def test_the_marketplace_lists_the_plugin():
    assert [p["name"] for p in MARKET["plugins"]] == [PLUGIN["name"]]
    assert MARKET["plugins"][0]["source"] == "./"


def test_versions_match_the_claude_code_plugin():
    assert PLUGIN["version"] == CC_PLUGIN["version"]
    assert MARKET["plugins"][0]["version"] == CC_PLUGIN["version"]


def test_hooks_run_the_launcher_for_their_own_event():
    for event, entries in HOOKS.items():
        assert [e["command"] for e in entries] == [
            'bash "${CURSOR_PLUGIN_ROOT}/scripts/hook.sh" --agent cursor '
            + event]


def test_every_hook_is_one_the_span_builder_reads():
    assert set(HOOKS) == set(cursor_spans._HANDLERS)


def test_nothing_cursor_ships_names_the_claude_plugin_root():
    for path in [ROOT / "cursor" / "hooks.json", ROOT / "cursor" / "mcp.json",
                 ROOT / ".cursor-plugin" / "plugin.json"] + COMMANDS:
        assert "CLAUDE_PLUGIN_ROOT" not in path.read_text(), path


def test_the_seven_commands_exist():
    assert [p.stem for p in COMMANDS] == [
        "rius-disable-here", "rius-enable-here", "rius-login", "rius-logout",
        "rius-off", "rius-on", "rius-status"]


def test_commands_run_rius_ctl_as_cursor():
    for path in COMMANDS:
        text = path.read_text()
        front = re.match(r"---\nname: (\S+)\ndescription: .+\n---\n", text)
        assert front and front.group(1) == path.stem, path
        assert "/scripts/rius_ctl.sh\" " in text and "--agent cursor" in text


def test_session_commands_use_the_id_session_start_exports():
    for name in ("rius-on", "rius-off", "rius-status"):
        text = (ROOT / "cursor" / "commands" / (name + ".md")).read_text()
        assert '--session "${%s:-}"' % cursor_hook.SESSION_ENV in text


def test_commands_fall_back_to_the_session_start_note():
    for path in COMMANDS:
        text = " ".join(path.read_text().split())
        assert "starting `%s`" % cursor_hook.CONTEXT_NOTE_PREFIX in text, path


def test_mcp_points_at_production_without_a_key():
    server = json.loads((ROOT / "cursor" / "mcp.json").read_text())["mcpServers"]["rius"]
    assert server == {"url": "https://mcp.eu.console.rius-glassflow.com/mcp"}
