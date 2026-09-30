# Contributing

Thanks for helping. Bug reports, fixes and docs improvements are all
welcome. For anything bigger than a small fix, open an issue first so we can
agree on the shape before you write it.

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
Security problems go through [SECURITY.md](SECURITY.md), not public issues.

## Setting up

The plugin is standard-library Python. Tests need two packages that never
ship with it:

```
git clone https://github.com/glassflow/rius-coding-agents.git
cd rius-coding-agents
python -m pip install 'pytest>=7.0' 'opentelemetry-proto==1.43.0'
python -m pytest -q
claude plugin validate .
```

To run your checkout inside Claude Code, add it as a local marketplace:

```
/plugin marketplace add /path/to/rius-coding-agents
/plugin install rius@rius-coding-agents
```

Claude Code caches the marketplace, so after every change run
`/plugin marketplace update rius-coding-agents` and `/reload-plugins`.
Skipping that makes a working change look broken, or the reverse.

To test against a real workspace without touching your own setup, sign in
to staging with `/rius:login --env staging`.

## Rules the code has to keep

CI enforces most of these, and a PR that breaks one won't merge.

- **Standard library only.** A Claude Code plugin can't ship a virtualenv,
  so nothing under `scripts/` may import a third-party package.
- **Python 3.9 is the floor.** It's whatever `python3` people already have.
- **Hooks never break Claude Code.** Every hook exits 0. Failures go to
  `~/.claude/rius/log/`, never to the user's session.
- **Default off, content optional.** Nothing is sent for a folder that
  isn't enabled, and `RIUS_CAPTURE_CONTENT=false` has to drop every content
  field, including in error paths.
- **No keys in logs, state files or commits.** The CI secret scan checks
  tracked files and history.

## Pull requests

- Keep a PR to one change, with a test that fails without it. If you fix a
  bug, say how you checked the test catches it.
- Update the README or `docs/` when behaviour a user can see changes, and
  add a line to `CHANGELOG.md` under the next version.
- Commit subjects are a type and a plain sentence, for example
  `fix: keep login-wait inside its budget`.
