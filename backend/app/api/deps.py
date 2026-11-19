from fastapi import Depends

from app.core.config import Settings, get_settings
from app.infrastructure.storage.local import LocalStorage


def get_storage(settings: Settings = Depends(get_settings)) -> LocalStorage:
    return LocalStorage(settings.storage_path)
