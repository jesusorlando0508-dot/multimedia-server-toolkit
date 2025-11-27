import logging
import logging.handlers
import os
import sys
import threading
from rich.console import Console
from rich.logging import RichHandler

from src.core.app_state import ui_queue
from src.core.config import config


class QueueLoggingHandler(logging.Handler):
    """Logging handler that forwards formatted log records to the UI queue.

    It classifies records: WARNING and above -> 'debug_error', lower -> 'debug_process'.
    """

    def __init__(self):
        super().__init__()

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
        except Exception:
            try:
                msg = f"{record.levelname}: {record.getMessage()}"
            except Exception:
                msg = "<unformattable log record>"
        try:
            if record.levelno >= logging.WARNING:
                ui_queue.put(("debug_error", msg))
            else:
                ui_queue.put(("debug_process", msg))
        except Exception:
            # avoid raising inside logging
            pass


def setup_ui_logging(rich_enabled: bool = True):
    """Attach UI logging handlers to the root logger and configure exception hooks.

    Settings taken from `config`:
    - debug_log_file
    - debug_log_max_bytes
    - debug_log_backup
    """
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    # Rotating file handler
    log_dir = config.get('debug_log_dir') or os.getcwd()
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception:
        log_dir = os.getcwd()
    log_file = os.path.join(log_dir, config.get('debug_log_file', 'debug.log'))
    max_bytes = int(config.get('debug_log_max_bytes', 5 * 1024 * 1024))
    backup = int(config.get('debug_log_backup', 5))

    try:
        fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup, encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        fh_formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        fh.setFormatter(fh_formatter)
        fh._preserve = True  # type: ignore[attr-defined]
        logger.addHandler(fh)
    except Exception:
        # ignore file handler errors
        pass

    # UI queue handler
    qh = QueueLoggingHandler()
    qh.setLevel(logging.DEBUG)
    qh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    qh._preserve = True  # type: ignore[attr-defined]
    logger.addHandler(qh)

    if rich_enabled:
        try:
            console = Console(theme=None, emoji=True, highlight=False)
            sh = RichHandler(console=console, show_path=False, log_time_format="%H:%M:%S")
            sh.setLevel(logging.INFO)
            logger.addHandler(sh)
        except Exception:
            pass
    else:
        try:
            sh = logging.StreamHandler(stream=sys.stdout)
            sh.setLevel(logging.INFO)
            sh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
            logger.addHandler(sh)
        except Exception:
            pass

    # Also capture uncaught exceptions
    def excepthook(exc_type, exc_value, exc_tb):
        try:
            logger.exception("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        except Exception:
            pass

    sys.excepthook = excepthook

    # For Python 3.8+, set threading.excepthook to capture exceptions in threads.
    try:
        orig_thread_excepthook = threading.excepthook
    except AttributeError:
        orig_thread_excepthook = None

    def thread_excepthook(args):
        try:
            logger.exception("Uncaught thread exception", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        except Exception:
            pass
        if orig_thread_excepthook:
            try:
                orig_thread_excepthook(args)
            except Exception:
                pass

    try:
        threading.excepthook = thread_excepthook
    except Exception:
        pass

    # Return logger for convenience
    return logger


def ui_log_process(message: str):
    """Helper to send a process-level message to the UI queue with timestamp."""
    try:
        import datetime
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ui_queue.put(("debug_process", f"{ts} {message}"))
    except Exception:
        pass


def ui_log_error(message: str):
    """Helper to send an error-level message to the UI queue with timestamp."""
    try:
        import datetime
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ui_queue.put(("debug_error", f"{ts} {message}"))
    except Exception:
        pass
