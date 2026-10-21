"""httpxのINFOログから動画CDNの署名クエリを除去します。"""
import logging
from urllib.parse import urlsplit, urlunsplit

import httpx


def _redact(value):
    if not isinstance(value, (str, httpx.URL)):
        return value
    try:
        parsed = urlsplit(str(value))
    except ValueError:
        return value
    host = (parsed.hostname or "").lower().rstrip(".")
    if host == "replicate.delivery" or host.endswith(".replicate.delivery") or host == "dnznrvs05pmza.cloudfront.net":
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return value


class _VideoURLFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(_redact(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _redact(value) for key, value in record.args.items()}
        return True


def install_httpx_filter():
    logger = logging.getLogger("httpx")
    if not any(isinstance(item, _VideoURLFilter) for item in logger.filters):
        logger.addFilter(_VideoURLFilter())
