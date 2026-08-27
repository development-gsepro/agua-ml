import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FMT = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

_LOG_LEVEL = logging.INFO
_LOG_TO_FILE = False


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(_LOG_LEVEL)

    fmt = logging.Formatter(
        _FMT,
        datefmt=_DATE_FMT,
    )

    console = logging.StreamHandler()
    console.setLevel(_LOG_LEVEL)
    console.setFormatter(fmt)

    logger.addHandler(console)

    if _LOG_TO_FILE:
        log_dir = Path(__file__).resolve().parent.parent / "logs"
        log_dir.mkdir(exist_ok=True)

        file_handler = RotatingFileHandler(
            filename=log_dir / f"{name}.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )

        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)

        logger.addHandler(file_handler)

    logger.propagate = False

    return logger