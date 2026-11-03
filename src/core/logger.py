import logging
import sys

def setup_logger(name: str = "manga_recap") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        
        formatter = logging.Formatter(
            '[%(asctime)s] %(levelname)s [%(name)s.%(funcName)s:%(lineno)d]: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # Console Handler routes exclusively to sys.stderr to maintain stdout purity
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(formatter)
        logger.addHandler(ch)
        
    return logger

def configure_logging(verbose: bool = False) -> None:
    """Configures log level and ensures stderr routing across all handlers."""
    logger = logging.getLogger("manga_recap")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            try:
                handler.stream = sys.stderr
            except Exception:
                pass

logger = setup_logger()

