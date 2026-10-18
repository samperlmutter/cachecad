import logging, sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def is_frozen() -> bool:
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def setup_logging(log_level=logging.DEBUG, log_filename="app.log"):
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / log_filename

    formatter = logging.Formatter(
        "%(asctime)s,%(msecs)03d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d:%H:%M:%S"
    )

    if is_frozen():
        mode = "a"
    else:
        mode = "w"
        log_file.write_text("", encoding="utf-8")

    file_handler = RotatingFileHandler(
        log_file,
        mode=mode,
        maxBytes=26214400,
        backupCount=1,
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(log_level)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)

    oauth_logger = logging.getLogger("oauth2_session")
    oauth_logger.propagate = False

    logger = logging.getLogger()
    logger.setLevel(log_level)
    logger.handlers.clear()
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    logging.getLogger("oauth2_session").setLevel(logging.CRITICAL + 1)
    logging.getLogger("oauthlib").setLevel(logging.CRITICAL + 1)
    logging.getLogger("requests_oauthlib").setLevel(logging.CRITICAL + 1)

    logging.debug("\n\nCacheCAD App Started\n\n")
    if is_frozen():
        logging.info("App running as EXE\n\n")
    else:
        logging.info("App running in Py Env\n\n")
