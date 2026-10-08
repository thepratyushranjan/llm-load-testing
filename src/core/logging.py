import logging
import sys


def setup_logging(service: str, machine_id: str, level: str = "INFO") -> logging.Logger:
    """Configure root logging and return the service logger"""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        f"%(asctime)s %(levelname)s [{machine_id}:{service}] %(message)s"
    ))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(service)
