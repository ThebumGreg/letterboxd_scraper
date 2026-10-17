import logging
import sys
from config import LOG_FILE_PATH, LOGS_DIR

def setup_logger(name: str = "letterboxd_scraper") -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    if not LOG_FILE_PATH.exists():
        LOG_FILE_PATH.touch()

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if not root_logger.handlers:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

        file_handler = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8", delay=False)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    logger_inst = logging.getLogger(name)
    logger_inst.setLevel(logging.INFO)
    return logger_inst

logger = setup_logger()

def get_latest_logs(max_chars: int = 12000) -> str:
    """Safely fetch the latest log entries for the web interface."""
    if LOG_FILE_PATH.exists():
        try:
            with open(LOG_FILE_PATH, "r", encoding="utf-8") as f:
                return f.read()[-max_chars:]
        except Exception as e:
            logger.error(f"Error reading log file: {e}")
            return f"Error reading log file: {e}"
    return "Log file initialized; pending execution logs..."