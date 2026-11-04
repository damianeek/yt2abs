"""``python -m yt2abs`` entry point (validates the config, then runs uvicorn)."""
from __future__ import annotations

import os
import sys


def main() -> None:
    import uvicorn

    from .app import create_app
    from .config import ConfigError, load_config

    try:
        # Validate before uvicorn binds the port: an invalid config (bad CIDR,
        # a role clash) must fail with a readable message instead of a uvicorn
        # traceback.  Build the app from this exact Config too: uvicorn's string
        # factory would import the module and call ``load_config()`` a second
        # time, duplicating every startup warning in the logs.
        config = load_config()
        app = create_app(config)
    except ConfigError as exc:
        print(f"ERROR: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    uvicorn.run(
        app,
        host=os.environ.get("YT2ABS_HOST", "127.0.0.1"),
        port=int(os.environ.get("YT2ABS_PORT", "8080")),
        log_level=os.environ.get("YT2ABS_LOG_LEVEL", "info").lower(),
        # The app owns proxy-header trust (``api.trusted_proxies``).  Uvicorn's
        # own ProxyHeadersMiddleware would rewrite ``scope.client`` from a
        # forged X-Forwarded-For before the middleware can check the peer, so a
        # direct caller could pick its own rate-limit bucket.
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
