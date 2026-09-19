"""Configuration: where it lives, what it may contain, and what it may not.

Three concerns, one import path. :mod:`~living_ink.config.paths` resolves the
config file, the data directory and the credentials directory;
:mod:`~living_ink.config.validate` holds the schema every ``config.yml`` is
checked against; :mod:`~living_ink.config.credentials` stores the secrets that
must never be in that file.

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
from living_ink.config.validate import (
    ACTIVE,
    CONFIG_SCHEMA,
    DEPRECATED,
    ERROR,
    FLAG,
    NUMBER,
    REMOVED,
    SCHEMA_VERSION,
    TEXT,
    WARNING,
    WHOLE,
    ConfigProblem,
    ConfigurationMissing,
    Key,
    Section,
    apply_status,
    split_problems,
    validate_config,
)

__all__ = [
    "ACTIVE",
    "AI_KEY_PREFIX",
    "CLOUD_TOKEN",
    "CONFIG_SCHEMA",
    "DEPRECATED",
    "ERROR",
    "FLAG",
    "NUMBER",
    "REMOVED",
    "SCHEMA_VERSION",
    "SSH_PASSWORD",
    "TEXT",
    "WARNING",
    "WHOLE",
    "ConfigProblem",
    "ConfigurationMissing",
    "Key",
    "Section",
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
    "split_problems",
    "validate_config",
    "write_secret",
]
