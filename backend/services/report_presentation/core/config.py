from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


BACKEND_DIR = Path(__file__).resolve().parents[3]
ENV_FILE = BACKEND_DIR / '.env'


class Settings(BaseSettings):
    max_docx_size_mb: float = Field(default=25, gt=0)
    max_uncompressed_size_mb: int = Field(default=250, gt=0)
    max_zip_entries: int = Field(default=10000, gt=0)
    storage_dir: Path = BACKEND_DIR / 'storage' / 'report_jobs'
    gemini_model: str = 'gemini-2.5-flash'
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra='ignore')


settings = Settings()
