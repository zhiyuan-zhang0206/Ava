"""Plugin access to declared, non-sensitive core configuration flags.

Plugins declare their Core dependencies in ``default_config.py`` ``contribute()``:

    def contribute() -> PluginContributions:
        return PluginContributions(
            flags=("daemon.notice_ttl_limit_seconds",)
        )

Config-face admission and SDK installation validate every declared key. Services
read from the declaration itself, without a second process-wide registry:

    from base.packages.plugins.flags import read_declared_flag

    ttl = read_declared_flag("daemon.notice_ttl_limit_seconds", contribute().flags)

Declaration is mandatory: ``read_declared_flag`` rejects a key absent from the
supplied declaration. Keys are fully qualified as ``<domain>.<field>``. The namespace is
every non-sensitive core Settings field; secrets remain in their existing secret
channels and are never flags.

Reads use the process's constructed Settings domains. An unavailable domain
fails explicitly; a read never constructs a second Settings image.
"""

from typing import Any

from base.host.env.config_lite_table import FIELD_DOMAINS, SENSITIVE_FIELDS
from base.host.env.config_registry import DOMAIN_ATTRS


class PluginFlagError(Exception):
    """Root of plugin core-flag declaration and read failures."""


class UnknownFlag(PluginFlagError):  # noqa: N818
    """A declared key is malformed, unknown, or names a sensitive core field."""


class UndeclaredFlag(PluginFlagError):  # noqa: N818
    """The current plugin tried to read a flag absent from its declaration."""


class FlagDomainUnavailable(PluginFlagError):  # noqa: N818
    """The running process profile did not construct a declared flag's domain."""


def validate_flag_key(key: str) -> str:
    """Validate one fully qualified, non-sensitive Settings key and return it."""
    if not isinstance(key, str) or key.count(".") != 1:
        raise UnknownFlag(f"unknown plugin flag {key!r}: flags must use exactly <domain>.<field>.")
    domain, field = key.split(".")
    if not domain or not field:
        raise UnknownFlag(f"unknown plugin flag {key!r}: flags must use exactly <domain>.<field>.")
    if domain not in DOMAIN_ATTRS:
        raise UnknownFlag(f"unknown plugin flag {key!r}: {domain!r} is not a Settings domain.")

    if FIELD_DOMAINS.get(field) != domain:
        raise UnknownFlag(
            f"unknown plugin flag {key!r}: {field!r} is not a field in the {domain!r} domain."
        )
    if field in SENSITIVE_FIELDS:
        raise UnknownFlag(f"unknown plugin flag {key!r}: secrets are not flags.")
    return key


def read_declared_flag(key: str, flags: tuple[str, ...]) -> Any:
    """Read a service's explicit pure-face Core dependency without an SDK registry.

    Secrets remain resources. Unavailable profile domains fail rather than
    constructing a second full Settings image on a plugin's behalf.
    """
    for declared in flags:
        validate_flag_key(declared)
    if key not in flags:
        raise UndeclaredFlag(f"core flag {key!r} is absent from the pure config declaration")
    domain, field = key.split(".")
    from base.config import settings

    if not settings.has_domain(domain):
        raise FlagDomainUnavailable(f"declared core flag {key!r} is unavailable in this profile")
    return getattr(getattr(settings, domain), field)
