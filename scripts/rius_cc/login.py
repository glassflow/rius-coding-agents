"""`/rius:login`: the portal agent-link flow.

The plugin asks the control plane for a link, the user opens it in the
portal, signs in and picks a workspace, and the plugin's poll then receives a
key for that workspace.

The flow is split in two because a slash command's output only reaches the
user once its script EXITS: a single blocking command would hide the code
the user needs until the wait was already over. `start` creates the link and
exits; `wait` polls and is run separately, in the background.

The device code between the two halves lives in its own 0600 file. The
resulting API key lives in `credentials.json`, also 0600 -- never in Claude
Code's settings, which get shared, committed and screenshotted.
"""
from __future__ import annotations

import contextlib
import http.client
import json
import os
import socket
import tempfile
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from rius_cc import platform_compat

# `wait` runs under Claude Code's Bash tool, which kills a command after ten
# minutes. Returning before that leaves the pending link on disk so a second
# `wait` can pick it up, instead of being killed mid-poll.
WAIT_BUDGET_SECONDS = 540
MAX_BACKOFF_SECONDS = 60
# The sign-in host rate-limits per IP, whatever interval the server names.
MIN_POLL_SECONDS = 2
REQUEST_TIMEOUT_SECONDS = 15.0
# Long enough for a superseded wait to notice at its next poll and let go.
LOCK_WAIT_SECONDS = 10.0
LOCK_RETRY_SECONDS = 0.25

ENVIRONMENTS = {
    "production": {
        "link_base": "https://connect.console.rius-glassflow.com",
        "console_url": "https://console.rius-glassflow.com",
        "mcp_url": "https://mcp.eu.console.rius-glassflow.com/mcp",
    },
    "staging": {
        "link_base": "https://connect.staging.rius.glassflow.xyz",
        "console_url": "https://staging.rius.glassflow.xyz",
        "mcp_url": "https://mcp.eu.staging.rius.glassflow.xyz/mcp",
    },
}
DEFAULT_ENVIRONMENT = "production"
ENVIRONMENT_VAR = "RIUS_ENV"

DISCLOSURE = (
    "Folders you enable send full sessions (prompts, replies, file contents, "
    "command output) to the workspace you pick. Everyone with access to that "
    "workspace, including its admins, can read them.")

LINK_EXPIRED = "That sign-in link expired. Run `/rius:login` again."
NO_SIGN_IN = "There is no sign-in in progress. Run `/rius:login` first."
ALREADY_WAITING = ("Another login is already waiting for approval in the "
                   "browser; it reports back when it finishes.")
SUPERSEDED = ("This sign-in was replaced by a newer /rius:login, which "
              "reports back instead.")

_PENDING_FIELDS = ("env", "link_id", "device_code", "user_code", "connect_url",
                   "interval", "expires_at")
_LINK_FIELDS = ("link_id", "device_code", "user_code", "connect_url", "interval",
                "expires_in")
_CREDENTIAL_FIELDS = ("api_key", "endpoint", "mcp_url", "workspace_id",
                      "workspace_name", "org_name", "email", "expires_at")
_REQUIRED_CREDENTIAL_FIELDS = ("api_key", "endpoint", "workspace_id",
                               "workspace_name", "email")
_NETWORK_ERRORS = (OSError, http.client.HTTPException)


class LoginError(Exception):
    """A terminal failure, phrased for the user."""


class WaitInProgress(Exception):
    """Another `wait` holds the pending link."""


class Superseded(Exception):
    """A newer `/rius:login` replaced the link this `wait` was polling."""


# --- HTTP -------------------------------------------------------------------

def _send(req: urllib.request.Request, timeout: float = REQUEST_TIMEOUT_SECONDS):
    """(status, parsed JSON body). Error statuses are data here, not
    exceptions: 428 is how the server says "not yet"."""
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


def post_json(url: str, payload: dict, bearer: Optional[str] = None):
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 method="POST", headers=headers)
    return _send(req)


def _is_success(status: int) -> bool:
    return 200 <= status < 300


def _missing(body: dict, fields) -> list:
    return [f for f in fields if body.get(f) in (None, "")]


def _detail(body: dict) -> str:
    return body.get("code") or body.get("detail") or body.get("title") or "no detail"


# --- Files ------------------------------------------------------------------

def _rius_dir(home: str) -> str:
    return os.path.join(home, ".claude", "rius")


def pending_path(home: str) -> str:
    return os.path.join(_rius_dir(home), "login_pending.json")


def credentials_path(home: str) -> str:
    return os.path.join(_rius_dir(home), "credentials.json")


def wait_lock_path(home: str) -> str:
    return os.path.join(_rius_dir(home), "login_wait.lock")


def _write_private(path: str, data: dict) -> None:
    """Write via a 0600 temp file and rename, so the secret is never readable
    by others, not even for the instant before a chmod."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".rius-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except BaseException:
        _remove(tmp)
        raise


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


def _is_current(home: str, pending: dict) -> bool:
    current = load_pending(home)
    return bool(current) and current["link_id"] == pending["link_id"]


def _remove_pending_if_still(home: str, pending: dict) -> None:
    if _is_current(home, pending):
        _remove(pending_path(home))


def load_pending(home: str) -> Optional[dict]:
    pending = _read_json(pending_path(home))
    if not pending or any(f not in pending for f in _PENDING_FIELDS):
        return None
    if pending["env"] not in ENVIRONMENTS:
        return None
    return pending


# --- Starting a link --------------------------------------------------------

def _link_base(env_name: str) -> str:
    return ENVIRONMENTS[env_name]["link_base"]


def choose_environment(flag: Optional[str], env) -> str:
    """`--env` beats RIUS_ENV beats production."""
    return flag or env.get(ENVIRONMENT_VAR) or DEFAULT_ENVIRONMENT


def _require_known(env_name: str) -> None:
    # A typo must not quietly sign the user in to production instead.
    if env_name not in ENVIRONMENTS:
        raise LoginError("Unknown environment %r. Choose one of: %s."
                         % (env_name, ", ".join(sorted(ENVIRONMENTS))))


def start(home: str, env_name: str = DEFAULT_ENVIRONMENT,
          post: Callable = post_json, now: Callable = time.time) -> dict:
    """Create an agent link and park it on disk for `wait`."""
    _require_known(env_name)
    url = _link_base(env_name) + "/v1/agent-links"
    try:
        status, body = post(url, {"client_name": socket.gethostname()[:64]})
    except _NETWORK_ERRORS as exc:
        raise LoginError("Could not reach Rius to start the sign-in (%s)." % exc)
    if not _is_success(status):
        raise LoginError("Rius could not start the sign-in (HTTP %s: %s)."
                         % (status, _detail(body)))
    missing = _missing(body, _LINK_FIELDS)
    if missing:
        raise LoginError("Rius started the sign-in but left out %s."
                         % ", ".join(missing))
    pending = {
        "env": env_name,
        "link_id": body["link_id"],
        "device_code": body["device_code"],
        "user_code": body["user_code"],
        "connect_url": body["connect_url"],
        "interval": int(body["interval"]),
        "expires_at": now() + int(body["expires_in"]),
    }
    _write_private(pending_path(home), pending)
    return pending


# --- Polling ----------------------------------------------------------------

def _poll_once(url: str, device_code: str, post: Callable):
    try:
        return post(url, {"device_code": device_code})
    except _NETWORK_ERRORS:
        return 0, {}


def _is_transient(status: int) -> bool:
    return status == 0 or status == 429 or status >= 500


def _backoff(interval: int, failures: int) -> float:
    return min(max(interval, MIN_POLL_SECONDS) * (2 ** failures),
               MAX_BACKOFF_SECONDS)


def poll_for_key(pending: dict, post: Callable = post_json,
                 sleep: Callable = time.sleep, now: Callable = time.time,
                 budget: Optional[float] = None,
                 is_current: Callable[[], bool] = lambda: True) -> Optional[dict]:
    """The token response, or None if the budget ran out while the link is
    still pending. Raises LoginError when the link is dead, Superseded when
    `is_current` says a newer link replaced it."""
    url = _link_base(pending["env"]) + "/v1/agent-links/token"
    if budget is None:
        budget = WAIT_BUDGET_SECONDS
    give_up_at = min(now() + budget, pending["expires_at"])
    failures = 0
    while now() < give_up_at:
        sleep(min(_backoff(pending["interval"], failures),
                  max(0.0, give_up_at - now())))
        if not is_current():
            raise Superseded(SUPERSEDED)
        status, body = _poll_once(url, pending["device_code"], post)
        if _is_success(status) and body.get("api_key"):
            _require_credentials(body)
            return body
        if status == 428:
            failures = 0
        elif _is_transient(status):
            failures += 1
        elif status in (404, 410):
            raise LoginError(LINK_EXPIRED)
        else:
            raise LoginError("Rius rejected the sign-in (HTTP %s: %s)."
                             % (status, _detail(body)))
    if now() >= pending["expires_at"]:
        raise LoginError(LINK_EXPIRED)
    return None


def _require_credentials(body: dict) -> None:
    missing = _missing(body, _REQUIRED_CREDENTIAL_FIELDS)
    if missing:
        raise LoginError("Rius issued a key but left out %s. Run `/rius:login` "
                         "again." % ", ".join(missing))


def _credentials(env_name: str, body: dict) -> dict:
    creds = {field: body.get(field) for field in _CREDENTIAL_FIELDS}
    creds["env"] = env_name
    return creds


def revoke(creds: dict, post: Callable = post_json) -> bool:
    """Best effort: True only when the server confirmed the key is dead.
    A 401 means the key no longer authenticates, so it is already gone.
    A key of an environment this plugin does not know is left alone: sending
    it to another environment's sign-in host would only leak it there."""
    if creds.get("env") not in ENVIRONMENTS:
        return False
    url = _link_base(creds["env"]) + "/v1/agent-keys/revoke"
    try:
        status, _ = post(url, {"workspace_id": creds.get("workspace_id")},
                         creds["api_key"])
    except _NETWORK_ERRORS:
        return False
    return _is_success(status) or status == 401


@contextlib.contextmanager
def _single_wait(home: str, sleep: Callable, now: Callable):
    """Two waits on one link would each mint a key, and the slower one could
    store a key the other's re-mint already revoked."""
    path = wait_lock_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = platform_compat.open_lock_file(path, mode=0o600)
    try:
        _take_lock(fd, sleep, now)
        try:
            yield
        finally:
            platform_compat.unlock(fd)
    finally:
        os.close(fd)


def _take_lock(fd: int, sleep: Callable, now: Callable) -> None:
    give_up_at = now() + LOCK_WAIT_SECONDS
    while not platform_compat.try_lock(fd):
        if now() >= give_up_at:
            raise WaitInProgress(ALREADY_WAITING)
        sleep(LOCK_RETRY_SECONDS)


def wait(home: str, post: Callable = post_json, sleep: Callable = time.sleep,
         now: Callable = time.time) -> Optional[dict]:
    """Finish a pending login. The stored credentials, or None if the user
    has not finished yet (the pending link is kept for another `wait`).

    Every terminal outcome removes the pending link unless a newer
    `/rius:login` has replaced it, and only success writes a credential.
    The key it replaces is revoked afterwards."""
    with _single_wait(home, sleep, now):
        return _wait_locked(home, post, sleep, now)


def _wait_locked(home: str, post: Callable, sleep: Callable,
                 now: Callable) -> Optional[dict]:
    pending = load_pending(home)
    if pending is None:
        raise LoginError(NO_SIGN_IN)
    try:
        body = poll_for_key(pending, post=post, sleep=sleep, now=now,
                            is_current=lambda: _is_current(home, pending))
    except LoginError:
        _remove_pending_if_still(home, pending)
        raise
    if body is None:
        return None
    previous = read_credentials(home)
    creds = _credentials(pending["env"], body)
    _write_private(credentials_path(home), creds)
    _remove_pending_if_still(home, pending)
    if previous and previous["api_key"] != creds["api_key"]:
        revoke(previous, post=post)
    return creds
