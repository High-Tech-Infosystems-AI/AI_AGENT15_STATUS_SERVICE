import logging
import logging.config
import os
import yaml
from pathlib import Path
from uuid import uuid4
# from app.core.config_dev  import settings
import os

ENV = os.getenv("APP_ENV", "dev")  # Default to 'dev'

if ENV == "prod":
    from .config_prod import settings
else:
    from .config_dev import settings

class ContextFilter(logging.Filter):
    def __init__(self, name='', run_id=None):

        super().__init__(name)
        self.run_id = run_id or uuid4()

    def filter(self, record):
        record.run_id = self.run_id
        return True
# from app.core import settings
def setup_logging(
    default_path='logging.yaml',
    default_level=logging.DEBUG,
    log_dir=settings.STATUS_AGENT_LOG
):
    try:
        # Ensure the log directory exists
        os.makedirs(log_dir, exist_ok=True)

        # Define the log file's full path
        log_file_path = os.path.join(log_dir, "status_service.log")

        # Load YAML configuration
        path = Path(default_path)
        if path.exists():
            with open(path, 'rt') as f:
                config = yaml.safe_load(f.read())

                # Dynamically update the file handler's filename
                if 'handlers' in config and 'file' in config['handlers']:
                    config['handlers']['file']['filename'] = log_file_path

                # Apply the updated logging configuration
                logging.config.dictConfig(config)
                logging.info(f"Logging configured using YAML file at {path}")
        else:
            # Fallback to basic configuration
            logging.basicConfig(
                level=default_level,
                format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                handlers=[
                    logging.FileHandler(log_file_path),
                    logging.StreamHandler()
                ]
            )
            logging.warning(f"Logging configuration file not found at {path}. Using basic config.")
        
    except Exception as e:
        # Log any errors during setup
        logging.basicConfig(level=default_level)
        logging.error(f"Error occurred during logging setup: {str(e)}", exc_info=True)
log = setup_logging()