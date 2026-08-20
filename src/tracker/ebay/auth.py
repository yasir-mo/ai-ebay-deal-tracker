"""OAuth2 client-credentials token, cached until just before it expires."""
from __future__ import annotations

import base64
import logging
import time

import requests

log = logging.getLogger(__name__)

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SCOPE = "https://api.ebay.com/oauth/api_scope"

#: Refresh this many seconds early so a token cannot expire mid-request.
EXPIRY_MARGIN = 60


class AuthError(Exception):
    """Raised when a token cannot be obtained.

    Subclassed from Exception rather than BrowseError to avoid a circular
    import; the scheduler catches both explicitly.
    """


class TokenProvider:
    def __init__(self, client_id: str, client_secret: str, session=None):
        self._client_id = client_id
        self._client_secret = client_secret
        self._session = session or requests.Session()
        self._token: str | None = None
        self._expires_at: float = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._expires_at - EXPIRY_MARGIN:
            return self._token
        return self._refresh()

    def _refresh(self) -> str:
        creds = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode()
        ).decode()
        resp = self._session.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": SCOPE},
            timeout=20,
        )
        if resp.status_code != 200:
            raise AuthError(
                f"token request failed: {resp.status_code} {resp.text[:300]}"
            )
        payload = resp.json()
        self._token = payload["access_token"]
        self._expires_at = time.time() + int(payload.get("expires_in", 7200))
        log.info("obtained eBay token, valid %ss", payload.get("expires_in"))
        return self._token
