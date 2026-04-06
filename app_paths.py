import os
from typing import Optional


_APP_ROOT = os.path.dirname(os.path.abspath(__file__))


def get_app_root() -> str:
    return _APP_ROOT


def resolve_app_path(path_value: Optional[str], default_name: str) -> str:
    candidate = str(path_value or '').strip() or str(default_name)
    if os.path.isabs(candidate):
        return candidate
    return os.path.join(_APP_ROOT, candidate)