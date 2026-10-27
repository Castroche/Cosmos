"""Origin checks for the loopback-only workbench; no pairing or session state."""

from urllib.parse import urlsplit


class LocalAccess:
    def __init__(self, origins: list[str]):
        self._origins = set(origins)

    def trusted_origin(self, origin: str | None, request_url: str) -> bool:
        if not origin or origin == "null":
            return False
        parsed = urlsplit(request_url)
        scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme)
        return origin in self._origins or origin == f"{scheme}://{parsed.netloc}"

    def trusted_request(
        self, origin: str | None, fetch_site: str | None, request_url: str
    ) -> bool:
        if origin is not None:
            return self.trusted_origin(origin, request_url)
        # Local scripts have no Origin/Fetch Metadata headers. Browser requests
        # from another site must not operate the local workbench.
        return fetch_site not in {"cross-site", "same-site"}
