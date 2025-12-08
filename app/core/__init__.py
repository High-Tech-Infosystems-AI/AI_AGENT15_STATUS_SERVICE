from .logging import log
import os

ENV = os.getenv("APP_ENV", "dev")  # Default to 'dev'

if ENV == "prod":
    from .config_prod import settings
else:
    from .config_dev import settings
__all__ = ["settings", "log"]