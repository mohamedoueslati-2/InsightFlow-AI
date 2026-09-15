"""Stage 1: technical validation before profiling or cleaning."""
from .technical import validate_file, ValidationFailure
from .ingestion import ingest_upload
