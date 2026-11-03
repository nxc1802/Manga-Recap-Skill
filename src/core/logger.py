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
        
        # Console Handler
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(formatter)
        logger.addHandler(ch)
        
        pass
        
    return logger

logger = setup_logger()
