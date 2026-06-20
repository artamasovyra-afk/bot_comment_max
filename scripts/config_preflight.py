from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config_validation import (  # noqa: E402
    BOT_COMMENT_MAX_CONFIG_FIELDS,
    format_validation_report,
    validate_bot_comment_max_config,
)


def main() -> int:
    values = {field.name: os.environ.get(field.name) for field in BOT_COMMENT_MAX_CONFIG_FIELDS}
    result = validate_bot_comment_max_config(values)
    print(format_validation_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
