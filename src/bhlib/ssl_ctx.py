from __future__ import annotations

import ssl
from pathlib import Path

import certifi


_BUNDLED_CA = Path(__file__).parent / "certs" / "booking_ca.pem"


def make_ssl_context(verify_ssl: bool = True) -> ssl.SSLContext:
    """Build an SSLContext that trusts certifi's roots plus the bundled
    GlobalSign GCC R3 DV TLS CA 2020 intermediate.

    booking.lib.buaa.edu.cn serves only the leaf cert (no intermediate chain),
    so we ship the intermediate ourselves rather than relying on AIA fetching.
    """
    if not verify_ssl:
        return ssl._create_unverified_context()  # noqa: SLF001
    ctx = ssl.create_default_context(cafile=certifi.where())
    if _BUNDLED_CA.exists():
        ctx.load_verify_locations(cafile=str(_BUNDLED_CA))
    return ctx
