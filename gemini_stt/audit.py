"""Structured diagnostics for local comparison archives; never inspect credentials."""
from datetime import datetime, timezone
import json
import logging
import math
import time

logger = logging.getLogger('bayaan.gemini.audit')


def emit(event, **fields):
    """Diagnostics must not change delivery or turn a successful stream into failure."""
    try:
        payload = dict(schema_version=1, event=event,
                       timestamp=datetime.now(timezone.utc).isoformat(),
                       monotonic_seconds=time.monotonic(), **fields)
        logger.info('GEMINI_AUDIT %s', json.dumps(payload, ensure_ascii=False, allow_nan=False))
    except Exception:
        try:
            logger.warning('Local audit record could not be serialized: %s', event)
        except Exception:
            pass  # Disk/handler failure must not stop speech delivery.
