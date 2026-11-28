"""Provider registry.

A provider is a module with: NAME, install(root, python, hook_script), uninstall(root),
collect(hook_input, ctx). Optional: follows_imports(path),
normalize(hook_input, event), skip_reason(hook_input), PROJECT_DIR_ENV. To add one,
create the module in this folder, register it in PROVIDERS below, add it to
COLLECTOR_FILES in faf_install.py, and add fixtures and tests under tests/.
"""
from . import claude_code, copilot

PROVIDERS = {claude_code.NAME: claude_code, copilot.NAME: copilot}
