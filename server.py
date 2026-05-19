"""DBSpowerController entrypoint.

The service lives in the dbs_controller/ package. This thin shim keeps the existing
systemd unit (which runs `python server.py`) valid. See README.md.
"""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "dbs_controller.app:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8787")),
    )
