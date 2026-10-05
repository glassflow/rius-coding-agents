---
name: rius-login
description: Sign in to Rius in the browser and pick the workspace this machine sends Codex traces to. Use when the user asks to log in to, connect or set up Rius.
---

# rius-login

This file is `<plugin root>/codex/skills/rius-login/SKILL.md`; the plugin root is
that path with `/codex/skills/rius-login/SKILL.md` removed from its end, so
`/x/rius/0.6.0/codex/skills/rius-login/SKILL.md` gives `/x/rius/0.6.0`.
Below, `<plugin root>` means that absolute path.

Both commands below call the Rius server and write the key under
`~/.codex/rius`, outside the workspace. Run each with escalated sandbox
permissions (`sandbox_permissions: "require_escalated"`), with the
justification "Rius sign-in needs the network and writes ~/.codex/rius".

1. Run, from the current working directory (add `--env staging` only if the
   user asked for staging):

   ```
   bash "<plugin root>/scripts/rius_ctl.sh" login --agent codex --cwd "$PWD"
   ```

2. Show the user its output verbatim, leaving out only the line that starts
   with `RIUS_LOGIN_PENDING:`. The `Open:` URL and the `Code:` must be visible
   now: the user needs them to approve the sign-in in the browser. Send this
   message BEFORE running anything else.

3. Then run exactly this command, with the same escalated permissions:

   ```
   bash "<plugin root>/scripts/rius_ctl.sh" login-wait --agent codex --cwd "$PWD"
   ```

   It waits for the browser approval for up to 9 minutes, so give it a long
   timeout (at least 600000 ms) and keep polling it while it is still
   running; do not start a second copy.

4. Report its output verbatim, leaving out any `RIUS_LOGIN_PENDING:` line. If
   it says `Still waiting`, run the same command again the same way, and keep
   doing so until it prints something else.

5. If it printed `Connected as`, tell the user they can run
   `$rius:rius-enable-here` in the folder they want traced. It is theirs to
   run: it chooses what that folder sends, so do not run it yourself.

If the sandbox refuses escalation, tell the user to run the command from
step 1 in their own terminal instead; it prints the same URL and code.
