"""`/rius:login`: Auth0's device authorization grant (RFC 8628), then one call
to Rius that turns the Auth0 token into an ingest+read API key.

The flow is split in two because of how `/rius` runs. The slash command's
output only reaches the user once its script EXITS, so a single blocking
command would hide the code the user needs until the wait was already over.
`start` fetches the code and exits; `wait` polls and is run separately.

The device code between the two halves lives in its own 0600 file. The
resulting API key lives in `credentials.json`, also 0600 -- never in Claude
Code's settings, which get shared, committed and screenshotted.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
CLIENT_NAME = "Claude Code (rius login)"

# `wait` runs under Claude Code's Bash tool, which kills a command after ten
# minutes. Returning before that leaves the pending code on disk so a second
# `wait` can pick it up, instead of being killed mid-poll.
WAIT_BUDGET_SECONDS = 540

# The only environment with a device-flow application today (P-RIUS-196):
# production's Auth0 tenant keeps its email-domain gate until this has been
# proven on staging. The client id is public by design -- a native client
# holds no secret.
ENVIRONMENTS = {
    "staging": {
        "auth0_domain": "glassflow-staging.eu.auth0.com",
        "client_id": "lNt2WxEfME3lPpibambn3o5Kl5hpZWsf",
        "audience": "https://cloud.glassflow.ai",
        "exchange_url": "https://device.staging.rius.glassflow.xyz/v1/device/exchange",
        "ingest_endpoint": "https://ingest.staging.rius.glassflow.xyz",
        "console_url": "https://staging.rius.glassflow.xyz",
    },
}
DEFAULT_ENVIRONMENT = "staging"

DISCLOSURE = """\
Signing in creates a Rius account (or uses your existing one) and stores an
API key for it in ~/.claude/rius/credentials.json.

Nothing is traced yet. Tracing stays OFF until you run `/rius:enable-here` in
a folder. For folders you enable, Rius receives the full session: your
prompts, Claude's replies, tool inputs and tool OUTPUT -- which includes the
contents of files Claude reads and the output of commands it runs.
Set RIUS_CAPTURE_CONTENT=false to send only structure and token counts."""


class LoginError(Exception):
    """A terminal failure, phrased for the user."""


# --- HTTP -------------------------------------------------------------------

def _send(req: urllib.request.Request, timeout: float = 15.0):
    """(status, parsed JSON body). Error statuses are data here, not
    exceptions: Auth0 reports `authorization_pending` as a 4xx."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, _parse(err.read())


def _parse(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def post_form(url: str, fields: dict):
    body = urllib.parse.urlencode(fields).encode("ascii")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    return _send(req)


def post_json(url: str, payload: dict, bearer: str):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 method="POST", headers={
                                     "Content-Type": "application/json",
                                     "Authorization": "Bearer " + bearer})
    return _send(req)


# --- Files ------------------------------------------------------------------

def _rius_dir(home: str) -> str:
    return os.path.join(home, ".claude", "rius")


def pending_path(home: str) -> str:
    return os.path.join(_rius_dir(home), "login_pending.json")


def credentials_path(home: str) -> str:
    return os.path.join(_rius_dir(home), "credentials.json")


def _write_private(path: str, data: dict) -> None:
    """Write via a 0600 temp file and rename, so the secret is never readable
    by others, not even for the instant before a chmod."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        os.remove(tmp)
    except OSError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _remove(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def read_credentials(home: str) -> Optional[dict]:
    creds = _read_json(credentials_path(home))
    if not creds or not isinstance(creds.get("api_key"), str):
        return None
    return creds


def clear_credentials(home: str) -> bool:
    return _remove(credentials_path(home))


# --- The flow ---------------------------------------------------------------

def start(home: str, env_name: str = DEFAULT_ENVIRONMENT,
          post: Callable = post_form, now: Callable = time.time) -> dict:
    """Ask Auth0 for a device code and park it on disk for `wait`."""
    env = ENVIRONMENTS[env_name]
    status, body = post(
        "https://%s/oauth/device/code" % env["auth0_domain"],
        {"client_id": env["client_id"], "audience": env["audience"],
         "scope": "openid email profile"})
    if status != 200 or "device_code" not in body:
        raise LoginError("Auth0 refused to start the sign-in (HTTP %s: %s)."
                         % (status, body.get("error_description")
                            or body.get("error") or "no detail"))
    pending = {
        "env": env_name,
        "device_code": body["device_code"],
        "user_code": body["user_code"],
        "verification_uri": body["verification_uri"],
        "verification_uri_complete": body.get("verification_uri_complete",
                                              body["verification_uri"]),
        "interval": int(body.get("interval", 5)),
        "expires_at": now() + int(body.get("expires_in", 900)),
    }
    _write_private(pending_path(home), pending)
    return pending


def load_pending(home: str) -> Optional[dict]:
    return _read_json(pending_path(home))


def poll_for_token(pending: dict, post: Callable = post_form,
                   sleep: Callable = time.sleep, now: Callable = time.time,
                   budget: float = WAIT_BUDGET_SECONDS) -> Optional[str]:
    """The access token, or None if the budget ran out while the user has
    still not answered. Raises LoginError on a denial or an expired code."""
    env = ENVIRONMENTS[pending["env"]]
    interval = pending["interval"]
    give_up_at = min(now() + budget, pending["expires_at"])
    while now() < give_up_at:
        sleep(interval)
        status, body = post(
            "https://%s/oauth/token" % env["auth0_domain"],
            {"grant_type": DEVICE_GRANT, "device_code": pending["device_code"],
             "client_id": env["client_id"]})
        if status == 200 and body.get("access_token"):
            return body["access_token"]
        error = body.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            # RFC 8628 3.5: every slow_down adds five seconds, for good.
            interval += 5
            continue
        if error == "access_denied":
            raise LoginError("Sign-in was declined in the browser.")
        if error == "expired_token":
            raise LoginError("The sign-in code expired before it was approved.")
        raise LoginError("Auth0 rejected the sign-in (HTTP %s: %s)."
                         % (status, body.get("error_description") or error
                            or "no detail"))
    if now() >= pending["expires_at"]:
        raise LoginError("The sign-in code expired before it was approved.")
    return None


def exchange(env_name: str, access_token: str, post: Callable = post_json) -> dict:
    """Trade the Auth0 token for a Rius API key, and learn where it landed."""
    env = ENVIRONMENTS[env_name]
    status, body = post(env["exchange_url"], {"client_name": CLIENT_NAME},
                        access_token)
    if not 200 <= status < 300 or not body.get("key"):
        raise LoginError("Rius could not issue a key for this account "
                         "(HTTP %s: %s)." % (status, body.get("detail")
                                             or body.get("title") or "no detail"))
    return {
        "api_key": body["key"],
        "endpoint": env["ingest_endpoint"],
        "env": env_name,
        "workspace_id": body.get("workspace_id"),
        "workspace_name": body.get("workspace_name"),
        "scopes": body.get("scopes") or [],
        "expires_at": body.get("expires_at"),
    }


def wait(home: str, post_token: Callable = post_form,
         post_exchange: Callable = post_json, sleep: Callable = time.sleep,
         now: Callable = time.time) -> Optional[dict]:
    """Finish a pending login. The stored credentials, or None if the user
    has not answered yet (the pending code is kept for another `wait`).

    Every terminal outcome -- success, denial, expiry -- removes the pending
    code, and only success writes a credential."""
    pending = load_pending(home)
    if pending is None:
        raise LoginError("There is no sign-in in progress. Run `/rius:login` first.")
    try:
        token = poll_for_token(pending, post=post_token, sleep=sleep, now=now)
        if token is None:
            return None
        creds = exchange(pending["env"], token, post=post_exchange)
    except LoginError:
        _remove(pending_path(home))
        raise
    _write_private(credentials_path(home), creds)
    _remove(pending_path(home))
    return creds
