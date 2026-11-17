"""Logging config for the server: uvicorn's defaults, with extension tokens redacted"""

import copy
import logging
import re

from uvicorn.config import LOGGING_CONFIG

TOKEN_RE = re.compile(r"((?:token|password)=)[^&\s\"']+")


class RedactTokens(logging.Filter):
    # uvicorn logs every WebSocket handshake with its full URL ("WebSocket /ws?token=<the password>..." [accepted])
    def filter(self, record):
        message = record.getMessage()
        if "token=" in message or "password=" in message:
            record.msg, record.args = TOKEN_RE.sub(r"\1<redacted>", message), ()
        return True


def log_config():
    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["redact_tokens"] = {"()": RedactTokens}
    for handler in config["handlers"].values():
        handler.setdefault("filters", []).append("redact_tokens")
    return config
