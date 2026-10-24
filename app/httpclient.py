"""HTTP clients for talking to other services.

Every request Releasomat sends says who it is (``User-Agent: Releasomat/<version> (+repo)``) and
gives up after a timeout. Tests never go online: they use saved answers (tests/conftest.py).
"""

import httpx

from app import REPOSITORY, __version__

USER_AGENT = f"Releasomat/{__version__} (+{REPOSITORY})"
TIMEOUT = httpx.Timeout(15.0, connect=10.0)


def client(**kwargs) -> httpx.AsyncClient:
    """An async client with Releasomat's User-Agent and timeouts. Use it with ``async with``."""
    headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
    kwargs.setdefault("timeout", TIMEOUT)
    kwargs.setdefault("follow_redirects", True)
    return httpx.AsyncClient(headers=headers, **kwargs)
