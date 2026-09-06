"""Secret store client — fetches secrets from TinyDBService via HTTP."""

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


def _default_url() -> str:
    host = os.getenv("TINYDB_HOST", "tinydb")
    port = os.getenv("TINYDB_PORT", "28080")
    return f"http://{host}:{port}"


class SecretStore:
    """Read-only client for TinyDBService credential store."""

    def __init__(
        self,
        tinydb_url: Optional[str] = None,
        api_key: Optional[str] = None,
        readonly: bool = False,
    ):
        """Initialize the secret store client.

        Args:
            tinydb_url: TinyDB service URL (default: from TINYDB_HOST/TINYDB_PORT)
            api_key: X-API-Key header value (default: from SECRET_STORE_TOKEN)
            readonly: Raise on any attempt to write
        """
        self.tinydb_url = (tinydb_url or _default_url()).rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv(
            "SECRET_STORE_TOKEN", ""
        )
        self.readonly = readonly
        self._cache: Optional[dict[str, str]] = None
        self._client = httpx.Client(timeout=10, verify=False)  # local network

    def get(self, key: str) -> str:
        """Fetch a secret by key. Raises KeyError if not found."""
        if self._cache is None:
            self._refresh()
        assert self._cache is not None
        if key not in self._cache:
            raise KeyError(f"Secret '{key}' not found")
        return self._cache[key]

    def get_optional(self, key: str) -> Optional[str]:
        """Fetch a secret by key, or None if not found."""
        try:
            return self.get(key)
        except KeyError:
            return None

    def put(self, key: str, value: str) -> None:
        """Store a secret. Raises ValueError if readonly."""
        if self.readonly:
            raise ValueError("SecretStore is read-only")

        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        resp = self._client.post(
            f"{self.tinydb_url}/key/",
            json={"key": key, "value": value},
            headers=headers,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Failed to store secret '{key}': {resp.status_code} {resp.text}"
            )

        # Invalidate cache
        self._cache = None
        logger.info("Stored secret %s", key)

    def refresh(self) -> None:
        """Refresh the secret cache."""
        self._refresh()

    def _refresh(self) -> None:
        """Fetch all secrets from TinyDB."""
        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        try:
            resp = self._client.get(
                f"{self.tinydb_url}/credentials",
                headers=headers,
                timeout=5,
            )
            if resp.status_code == 200:
                self._cache = resp.json()
                logger.debug("Refreshed %d secrets", len(self._cache))
            else:
                logger.warning(
                    "Failed to refresh secrets: %s %s", resp.status_code, resp.text
                )
                self._cache = {}
        except httpx.TimeoutException:
            logger.warning("Timeout refreshing secrets from TinyDB")
            self._cache = {}
        except httpx.ConnectError:
            logger.warning("Cannot connect to TinyDB service at %s", self.tinydb_url)
            self._cache = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._client.close()
