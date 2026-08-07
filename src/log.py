"""Single logging helper for the whole project.

Every module gets its logger via get_logger(). Console shows the plain
message; logs/wnba.log keeps timestamped plain text.
"""

import logging
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

_configured = False


def get_logger(name: str = "wnba") -> logging.Logger:
    global _configured
    if not _configured:
        LOG_DIR.mkdir(exist_ok=True)
        root = logging.getLogger("wnba")
        root.setLevel(logging.INFO)

        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(console)

        file = logging.FileHandler(LOG_DIR / "wnba.log", encoding="utf-8")
        file.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(file)

        _configured = True

    return logging.getLogger(name if name.startswith("wnba") else f"wnba.{name}")
