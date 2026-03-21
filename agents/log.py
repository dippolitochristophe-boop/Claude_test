"""
Shared logger for all agents and scrapers.

Console : INFO level  (human-readable during runs)
File    : DEBUG level → <project_root>/run.log  (exhaustif, écrasé à chaque run)

Usage :
    from agents.log import get_logger, init_run_log
    init_run_log()          # une seule fois au démarrage du script principal
    logger = get_logger("mon_module")
    logger.debug("détail interne")
    logger.info("message console")
"""

import logging
import os
from pathlib import Path

# Racine du projet = parent du dossier agents/
_PROJECT_ROOT = Path(__file__).parent.parent
LOG_FILE = str(_PROJECT_ROOT / "run.log")


def init_run_log() -> str:
    """
    Écrase run.log et démarre un nouveau run propre.
    À appeler UNE SEULE FOIS au tout début du script principal (avant tout get_logger).
    Retourne le chemin absolu du fichier log.
    """
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        from datetime import datetime
        f.write(f"# run.log — démarré le {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("# Copier-coller ce fichier entier pour le diagnostic Claude\n\n")
    return LOG_FILE


def get_logger(name: str = "scraper") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured — avoid duplicate handlers on reimport

    logger.setLevel(logging.DEBUG)

    # Console handler — INFO et au-dessus (vue propre)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)s %(message)s"))

    # File handler — DEBUG et au-dessus (tout le détail)
    fh = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    ))

    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger
