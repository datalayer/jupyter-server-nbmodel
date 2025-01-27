from jupyter_events import EventLogger
import pathlib

_JUPYTER_SERVER_EVENTS_URI = "https://events.jupyter.org/jupyter_server_nbmodel"
_DEFAULT_EVENTS_SCHEMA_PATH = pathlib.Path(__file__).parent / "event_schemas"

class _EventLogger:
    _event_logger = None

    @classmethod
    def init_event_logger(cls) -> EventLogger:
        """Initialize or return the existing Event Logger."""
        if cls._event_logger is None:
            cls._event_logger = EventLogger()
            schema_ids = [
                "https://events.jupyter.org/jupyter_server_nbmodel/cell_execution/v1",
            ]
            for schema_id in schema_ids:
                rel_schema_path = schema_id.replace(_JUPYTER_SERVER_EVENTS_URI + "/", "") + ".yaml"
                schema_path = _DEFAULT_EVENTS_SCHEMA_PATH / rel_schema_path
                cls._event_logger.register_event_schema(schema_path)
        return cls._event_logger


event_logger = _EventLogger.init_event_logger()
