"""Load ai-accounts from its vendored submodule.

The account switcher lives in its own repository,
https://github.com/shantamg/ai-accounts, checked out at vendor/ai-accounts.
This shim keeps ``import ai_accounts`` working for the rest of Telegram
Control by registering the submodule's module under this name.
"""

import importlib.util
import sys
from pathlib import Path

_SOURCE = Path(__file__).resolve().parent / "vendor" / "ai-accounts" / "ai_accounts.py"
if not _SOURCE.is_file():
    raise ImportError(
        f"{_SOURCE} is missing. Run `git submodule update --init` in the Telegram Control checkout."
    )
_spec = importlib.util.spec_from_file_location(__name__, _SOURCE)
_module = importlib.util.module_from_spec(_spec)
sys.modules[__name__] = _module
_spec.loader.exec_module(_module)
