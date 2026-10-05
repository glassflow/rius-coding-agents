"""The test seam for a key: a /rius:login credential in the test's own HOME.

The environment no longer supplies a key or an endpoint, so a test that
needs tracing to reach a (fake) server signs in the way a user does.
"""
from rius_cc import login

TEST_KEY = "glassflow_k"
TEST_ENDPOINT = "https://ingest.eu.console.rius-glassflow.com"


def sign_in(home, api_key=TEST_KEY, endpoint=TEST_ENDPOINT, env="production",
            **fields):
    creds = {"api_key": api_key, "endpoint": endpoint, "env": env,
             "workspace_id": "ws-test", "workspace_name": "test-workspace",
             "email": "dev@example.com"}
    creds.update(fields)
    login._write_private(login.credentials_path(str(home)), creds)
    return creds
