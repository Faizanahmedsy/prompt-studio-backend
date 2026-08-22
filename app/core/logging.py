import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    """One stream handler on the root logger, in a format that greps cleanly."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    for existing in list(root.handlers):
        root.removeHandler(existing)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)

    # uvicorn installs its own handlers; let them bubble to ours instead so the
    # access log and the application log share one format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
