"""
Shared logger for all agents.
Console: INFO level (human-readable during runs)
File:    DEBUG level → tempdir/scraper.log (full trace for diagnostics)
"""

import logging
import os
import tempfile

LOG_FILE = os.path.join(tempfile.gettempdir(), "scraper.log")


def get_logger(name: str = "scraper") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured — avoid duplicate handlers on reimport

    logger.setLevel(logging.DEBUG)

    # Console handler — INFO and above
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)s %(message)s"))

    # File handler — DEBUG and above
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S"
    ))

    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger
