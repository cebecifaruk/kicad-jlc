"""Plugin settings (table columns, filter, options) stored as JSON.

The file lives in the folder KiCad reserves for this plugin's settings
(KiCad.get_plugin_settings_path), so it survives plugin updates.
"""

import json
import os
from typing import Any, Dict, Optional

FILE_NAME = "settings.json"

# plugin.json is in the folder above src/, both in the repository and once installed.
PLUGIN_JSON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin.json"
)


def plugin_identifier() -> str:
    """The plugin identifier from plugin.json, e.g. "com.github.cebecifaruk.kicad-jlc"."""
    with open(PLUGIN_JSON, encoding="utf-8") as f:
        return json.load(f)["identifier"]


class Settings:
    def __init__(self, directory: Optional[str]):
        self.path = os.path.join(directory, FILE_NAME) if directory else None
        self.data: Dict[str, Any] = {}
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.data = loaded
            except (OSError, ValueError):
                self.data = {}  # unreadable or corrupt: start from defaults

    def get(self, key: str, default: Any) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def save(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            temporary = self.path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(temporary, self.path)
        except OSError:
            pass  # settings are a convenience; never fail the plugin over them
