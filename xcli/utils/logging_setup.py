"""
Logging configuration for the application using Loguru.

This module provides a centralized logging configuration that can be imported and used
across the entire application. It sets up both file and console logging with
colored output and log rotation.
"""

import sys
from pathlib import Path
from typing import Optional, Union, Any
from loguru import logger

# Log directory
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True, parents=True)

# Default log file path
DEFAULT_LOG_FILE = LOG_DIR / "app.log"

# Default log level (TRACE < DEBUG < INFO < SUCCESS < WARNING < ERROR < CRITICAL)
DEFAULT_LEVEL = "INFO"

# Maximum log file size (10MB)
MAX_BYTES = 10 * 1024 * 1024

# Number of backup files to keep
BACKUP_COUNT = 5

# Custom log format with colors
LOG_FORMAT = (
    "<green>{time:HH:mm:ss}</green> | "
    "<level>{level}</level> | "
    "<cyan>{extra[name]}</cyan>:<cyan>{line}</cyan> | "
    "<level>{message}</level>"
)


def setup_logger(
    name: Optional[str] = None,
    log_file: Optional[Union[str, Path]] = None,
    level: str = DEFAULT_LEVEL,
    format_str: str = LOG_FORMAT,
    rotation: str = f"{MAX_BYTES} MB",
    retention: str = f"{BACKUP_COUNT} days",
    **kwargs: Any,
):
    """
    Set up and configure a Loguru logger.

    Args:
        name: Name of the logger. If None, uses the root logger.
        log_file: Path to the log file. If None, logs only to console.
        level: Logging level (e.g., "INFO", "DEBUG", "WARNING").
        format_str: Log message format string with color tags.
        rotation: When to rotate the log file (e.g., "10 MB", "1 day").
        retention: How long to keep old log files (e.g., "1 week", "3 months").
        **kwargs: Additional arguments to pass to logger.add().

    Returns:
        Configured Loguru logger instance.
    """
    # Remove default handler
    logger.remove()

    # Add console handler with colors
    logger.add(
        sys.stderr,
        level=level,
        format=format_str,
        colorize=True,
        **kwargs
    )

    # Add file handler if log_file is provided
    if log_file:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)

        logger.add(
            str(log_file),
            level=level,
            format=format_str,
            rotation=rotation,
            retention=retention,
            encoding="utf-8",
            **kwargs
        )

    # Return a bound logger if name is provided, otherwise return the root logger
    if name:
        return logger.bind(name=name)
    return logger


def get_logger(name: Optional[str] = None):
    """
    Get a configured Loguru logger instance.

    This is a convenience function that uses the default configuration.

    Args:
        name: Name of the logger. If None, returns the root logger.

    Returns:
        Configured Loguru logger instance.
    """
    setup_logger(name=name)

    if name:
        return logger.bind(name=name)
    return logger


if __name__ == "__main__":
    log = get_logger('test')
    log.trace("This is a trace message")
    log.debug("This is a debug message")
    log.info("This is an info message")
    log.success("This is a success message")
    log.warning("This is a warning message")
    log.error("This is an error message")
    log.critical("This is a critical message")

