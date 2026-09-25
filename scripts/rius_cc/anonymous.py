"""Anonymous workspaces (P-RIUS-196, model B).

`provision` creates a memberless workspace plus an ingest+read key with no
sign-in, so tracing can start at once. The response carries a one-time claim
token: whoever later signs in with it attaches the key, and the traces sent
so far, to a workspace their account can access. The key itself never
changes, so a claim needs nothing reconfigured.

The claim token is a bearer secret. It lives only in `credentials.json`
(0600) and is shown to the user only inside the claim URL.
"""
from __future__ import annotations

import os
import time
from typing import Callable, List, Optional, Tuple

from . import login
from .login import LoginError

CLIENT_NAME = "Claude Code (rius-coding-agents)"

# Auth0 normally states how long its token lives; this is only the fallback
# for how long a parked claim choice may reuse it.
DEFAULT_TOKEN_SECONDS = 600


class SignInExpired(LoginError):
    """The Auth0 token parked for a claim choice is no longer usable."""


def claim_pending_path(home: str) -> str:
    return os.path.join(os.path.dirname(login.credentials_path(home)),
                        "claim_pending.json")


def is_unclaimed(creds: Optional[dict]) -> bool:
    return bool(creds and creds.get("anonymous"))


def _api(env_name: str, path: str) -> str:
    return login.ENVIRONMENTS[env_name]["api_base"] + path


def _detail(body) -> str:
    if isinstance(body, dict):
        return body.get("detail") or body.get("title") or "no detail"
    return "no detail"


# --- Provision ----------------------------------------------------------------

def provision(home: str, env_name: str = login.DEFAULT_ENVIRONMENT,
              post: Callable = login.post_json_noauth) -> dict:
    """Create an anonymous workspace and store its key. Nothing is stored on
    any failure."""
    try:
        status, body = post(_api(env_name, "/v1/anonymous/workspaces"),
                            {"client_name": CLIENT_NAME})
    except OSError as exc:
        raise LoginError("could not reach Rius (%s)." % exc)
    if status == 429:
        raise LoginError("new workspaces from this address are "
                         "rate-limited; try again later.")
    required = ("key", "claim_token", "claim_url")
    if not 200 <= status < 300 or not all(body.get(f) for f in required):
        raise LoginError("the server answered HTTP %s: %s."
                         % (status, _detail(body)))
    creds = {
        "api_key": body["key"],
        "anonymous": True,
        "claim_token": body["claim_token"],
        "claim_url": body["claim_url"],
        "endpoint": login.ENVIRONMENTS[env_name]["ingest_endpoint"],
        "env": env_name,
        "workspace_id": body.get("workspace_id"),
        "workspace_name": body.get("workspace_name"),
        "scopes": body.get("scopes") or [],
        "expires_at": body.get("expires_at"),
    }
    login._write_private(login.credentials_path(home), creds)
    return creds


# --- Claim ----------------------------------------------------------------------

def targets(env_name: str, access_token: str,
            get: Callable = login.get_json) -> List[dict]:
    """The workspaces this Auth0 identity may claim into."""
    status, body = get(_api(env_name, "/v1/claims/targets"), access_token)
    if not 200 <= status < 300 or not isinstance(body, list):
        raise LoginError("Rius could not list your workspaces (HTTP %s: %s)."
                         % (status, _detail(body)))
    return body


def _mark_claimed(creds: dict, fields: dict) -> dict:
    for secret in ("claim_token", "claim_url"):
        creds.pop(secret, None)
    creds["anonymous"] = False
    creds.update(fields)
    return creds


def claim(home: str, access_token: str, workspace_id: str,
          post: Callable = login.post_json) -> dict:
    """Attach the stored anonymous key to `workspace_id`. The stored
    credential keeps its key and learns its new workspace."""
    creds = login.read_credentials(home)
    if not is_unclaimed(creds):
        raise LoginError("The stored key is not an unclaimed workspace, so "
                         "there is nothing to claim.")
    env_name = creds.get("env") or login.DEFAULT_ENVIRONMENT
    status, body = post(_api(env_name, "/v1/claims"),
                        {"claim_token": creds["claim_token"],
                         "workspace_id": workspace_id}, access_token)
    if status == 404:
        raise LoginError("The claim link is invalid, or your account cannot "
                         "access that workspace.")
    if status == 409:
        # The token is spent (typically through the claim URL in a browser):
        # the key already works wherever it went, we just do not know where.
        login._write_private(login.credentials_path(home), _mark_claimed(
            creds, {"workspace_id": None, "workspace_name": None}))
        raise LoginError("This workspace was already claimed. The key keeps "
                         "working in the workspace it was claimed into.")
    if not 200 <= status < 300:
        raise LoginError("Rius could not claim the workspace (HTTP %s: %s)."
                         % (status, _detail(body)))
    _mark_claimed(creds, {
        "workspace_id": body.get("workspace_id") or workspace_id,
        "workspace_name": body.get("workspace_name"),
        "org_name": body.get("org_name"),
    })
    login._write_private(login.credentials_path(home), creds)
    return creds


def write_claim_pending(home: str, token: dict, choices: List[dict],
                        now: Callable = time.time) -> None:
    lifetime = int(token.get("expires_in", DEFAULT_TOKEN_SECONDS))
    login._write_private(claim_pending_path(home), {
        "access_token": token["access_token"],
        "expires_at": now() + lifetime,
        "targets": choices,
    })


def after_login(home: str, token: dict, get: Callable = login.get_json,
                post: Callable = login.post_json, now: Callable = time.time
                ) -> Tuple[Optional[dict], Optional[List[dict]]]:
    """(claimed credentials, None) when there was a single target, else
    (None, the targets to choose from, parked for `finish_claim`)."""
    creds = login.read_credentials(home) or {}
    choices = targets(creds.get("env") or login.DEFAULT_ENVIRONMENT,
                      token["access_token"], get=get)
    if not choices:
        raise LoginError("Your account has no workspace to claim this one into.")
    if len(choices) == 1:
        return claim(home, token["access_token"], choices[0]["workspace_id"],
                     post=post), None
    write_claim_pending(home, token, choices, now=now)
    return None, choices


def pick(choices: List[dict], choice: str) -> Optional[dict]:
    """A target by 1-based number, workspace id, or unambiguous name."""
    choice = choice.strip()
    if choice.isdigit():
        n = int(choice)
        return choices[n - 1] if 1 <= n <= len(choices) else None
    by_id = [t for t in choices if t.get("workspace_id") == choice]
    by_name = [t for t in choices
               if (t.get("workspace_name") or "").lower() == choice.lower()]
    matches = by_id or by_name
    return matches[0] if len(matches) == 1 else None


def format_targets(choices: List[dict]) -> str:
    return "\n".join("  %d. %s (%s, %s)" % (i, t.get("workspace_name"),
                                            t.get("org_name"), t.get("role"))
                     for i, t in enumerate(choices, 1))


def finish_claim(home: str, choice: str, post: Callable = login.post_json,
                 now: Callable = time.time) -> dict:
    """Claim into the target the user chose from a parked list."""
    path = claim_pending_path(home)
    pending = login._read_json(path)
    if pending is None:
        raise LoginError("No claim is waiting for a choice. Run `/rius login` "
                         "first.")
    if now() >= pending.get("expires_at", 0):
        login._remove(path)
        raise SignInExpired("The sign-in for this claim has expired.")
    choices = pending.get("targets") or []
    target = pick(choices, choice)
    if target is None:
        raise LoginError("No workspace matches %r. Choose one of:\n%s"
                         % (choice, format_targets(choices)))
    try:
        creds = claim(home, pending["access_token"], target["workspace_id"],
                      post=post)
    except LoginError:
        if not is_unclaimed(login.read_credentials(home)):
            login._remove(path)
        raise
    login._remove(path)
    return creds


def forget_pending(home: str) -> None:
    login._remove(claim_pending_path(home))
