#!/usr/bin/env python3
"""Run the extension server (behind nginx in production)"""

import uvicorn

from lancers_bot.config import SERVER_HOST, SERVER_PORT
from lancers_bot.logs import log_config

if __name__ == "__main__":
    # The extension sends its token in the WebSocket URL's query string: no access log, and
    # the handshake lines uvicorn still logs are redacted
    uvicorn.run("lancers_bot.server:app", host=SERVER_HOST, port=SERVER_PORT, log_config=log_config(),
                access_log=False, proxy_headers=True, forwarded_allow_ips="127.0.0.1")
