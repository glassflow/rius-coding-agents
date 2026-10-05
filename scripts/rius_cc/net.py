"""The one way this plugin opens a URL that carries a key.

Two rules, both about where a bearer key can end up:

* https only. Plain http is allowed for a loopback host, where the bytes
  never leave the machine, so local receivers and the test suite still work.
* No redirects. urllib's default handler follows a 301/302/303 to any host
  and re-sends the Authorization header, so one open redirect would hand the
  key to whoever it points at. A redirect comes back as an HTTPError with its
  3xx status instead.
"""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request

LOOPBACK_HOSTS = frozenset(("localhost", "127.0.0.1", "::1"))


class InsecureURL(urllib.error.URLError):
    """Refused before connecting. A URLError, so every caller's existing
    network-error handling already covers it."""


def is_allowed_url(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        return bool(parts.hostname)
    return parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_RefuseRedirect)


def urlopen(req: urllib.request.Request, timeout: float):
    if not is_allowed_url(req.full_url):
        raise InsecureURL("refusing a non-https URL for a non-local host")
    return _OPENER.open(req, timeout=timeout)
