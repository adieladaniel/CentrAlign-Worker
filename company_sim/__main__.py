"""`python -m company_sim` starts the simulator on port 8001."""
import logging
import re

import uvicorn

from . import config

_ENTRY_PATH = re.compile(r"/t/[^/?#\s]+")


def hide_entry_tokens() -> None:
    """Keep tenant entry tokens (``/t/<token>/``, a login-equivalent secret) out of the access log."""
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        if record.name == "uvicorn.access" and isinstance(record.args, tuple):
            record.args = tuple(_ENTRY_PATH.sub("/t/[hidden]", a) if isinstance(a, str) else a for a in record.args)
        return record

    logging.setLogRecordFactory(factory)


if __name__ == "__main__":
    hide_entry_tokens()
    uvicorn.run("company_sim.app:app", host=config.HOST, port=config.PORT, log_level="info")
