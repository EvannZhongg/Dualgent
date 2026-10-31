from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    api_key: str
    model: str = "deepseek-flash"
    api_url: str = "https://api.deepseek.com"
    port: int = 8765

    @classmethod
    def load(cls, data_dir: Path, port: int = 8765) -> Settings:
        load_dotenv(Path.cwd() / ".env", override=False)
        return cls(
            data_dir=data_dir.resolve(),
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            model=os.environ.get("DUALGENT_MODEL", "deepseek-flash"),
            api_url=os.environ.get("DUALGENT_API_URL", "https://api.deepseek.com"),
            port=port,
        )
