"""Configuration: where it lives, what it may contain, and what it may not.

Four concerns, one import path. :mod:`~living_ink.config.paths` resolves the
config file, the data directory and the credentials directory;
:mod:`~living_ink.config.schema` declares every setting there is;
:mod:`~living_ink.config.validate` checks a parsed ``config.yml`` against that
declaration; :mod:`~living_ink.config.credentials` stores the secrets that must
never be in the file.

Everything the rest of the package used to import from ``living_ink.config``
is re-exported here, so a caller that only wants ``get_config_path`` does not
have to know which module grew it.
"""

from living_ink.config.credentials import (
    AI_KEY_PREFIX,
    CLOUD_TOKEN,
    SSH_PASSWORD,
    ai_key_name,
    configured_ai_providers,
    delete_secret,
    list_secrets,
    mask,
    read_secret,
    write_secret,
)
from living_ink.config.paths import (
    credentials_dir,
    find_repo_root,
    get_config_path,
    get_data_dir,
    get_logs_dir,
)
from living_ink.config.schema import (
    ACTIVE,
    BY_FIELD,
    CHOICE,
    DEPRECATED,
    FLAG,
    LEGACY_KEYS,
    LIST,
    NUMBER,
    PATH,
    REMOVED,
    SECRET,
    SECTION_KEYS,
    SECTIONS,
    SETTINGS,
    STORE_CONFIG,
    STORE_CREDENTIALS,
    STORE_ENV_ONLY,
    TEXT,
    WHOLE,
    Choice,
    Section,
    Setting,
    settings_for_section,
)
from living_ink.config.validate import (
    ERROR,
    SCHEMA_VERSION,
    WARNING,
    ConfigProblem,
    ConfigurationMissing,
    apply_status,
    reads_as,
    split_problems,
    validate_config,
)

__all__ = [
    "ACTIVE",
    "AI_KEY_PREFIX",
    "BY_FIELD",
    "CHOICE",
    "CLOUD_TOKEN",
    "DEPRECATED",
    "ERROR",
    "FLAG",
    "LEGACY_KEYS",
    "LIST",
    "NUMBER",
    "PATH",
    "REMOVED",
    "SCHEMA_VERSION",
    "SECRET",
    "SECTIONS",
    "SECTION_KEYS",
    "SETTINGS",
    "SSH_PASSWORD",
    "STORE_CONFIG",
    "STORE_CREDENTIALS",
    "STORE_ENV_ONLY",
    "TEXT",
    "WARNING",
    "WHOLE",
    "Choice",
    "ConfigProblem",
    "ConfigurationMissing",
    "Section",
    "Setting",
    "ai_key_name",
    "apply_status",
    "configured_ai_providers",
    "credentials_dir",
    "delete_secret",
    "find_repo_root",
    "get_config_path",
    "get_data_dir",
    "get_logs_dir",
    "list_secrets",
    "mask",
    "read_secret",
    "reads_as",
    "settings_for_section",
    "split_problems",
    "validate_config",
    "write_secret",
]
