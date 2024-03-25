import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import marshmallow_dataclass
from marshmallow import ValidationError, post_load, pre_load
from pygitguardian.models import FromDictMixin

from ggshield.core.config.utils import (
    find_global_config_path,
    find_local_config_path,
    load_yaml_dict,
    remove_common_dict_items,
    replace_in_keys,
    save_yaml_dict,
    update_dict_from_other,
)
from ggshield.core.constants import DEFAULT_LOCAL_CONFIG_PATH
from ggshield.core.errors import ParseError, UnexpectedError, format_validation_error
from ggshield.core.text_utils import display_warning
from ggshield.core.types import FilteredConfig, IgnoredMatch
from ggshield.core.url_utils import api_to_dashboard_url


logger = logging.getLogger(__name__)
CURRENT_CONFIG_VERSION = 2

_IGNORE_KNOWN_SECRETS_KEY = "ignore-known-secrets"

GHSA_ID_PATTERN = re.compile("GHSA(-[a-zA-Z0-9]{4}){3}")
POLICY_ID_PATTERN = re.compile("GG_IAC_[0-9]{4}")


@marshmallow_dataclass.dataclass
class SecretConfig(FilteredConfig):
    """
    Holds all user-defined secret-specific settings
    """

    show_secrets: bool = False
    ignored_detectors: Set[str] = field(default_factory=set)
    ignored_matches: List[IgnoredMatch] = field(default_factory=list)
    ignored_paths: Set[str] = field(default_factory=set)
    ignore_known_secrets: bool = False

    def add_ignored_match(self, secret: IgnoredMatch) -> None:
        """
        Add secret to ignored_matches.
        """
        for match in self.ignored_matches:
            if match.match == secret.match:
                # take the opportunity to name the ignored match
                if not match.name:
                    match.name = secret.name
                return
        self.ignored_matches.append(secret)


def validate_policy_id(policy_id: str) -> bool:
    return bool(POLICY_ID_PATTERN.fullmatch(policy_id))


@marshmallow_dataclass.dataclass
class ConfigIgnoredElement(FilteredConfig):
    """
    Base class for a config element which can be ignored temporarily.
    Classes inheriting it must reject expired dates themselves.
    """

    comment: Optional[str]
    until: Optional[datetime]

    # Accept date yyyy-mm-dd instead of a full datetime
    @pre_load
    def parse_date(self, data: Dict[str, Any], **kwargs: Any):
        if not (isinstance(data, dict)) or data.get("until") is None:
            return data
        try:
            data["until"] = str(datetime.strptime(str(data["until"]), "%Y-%m-%d"))
            return data
        except ValueError:
            return data

    @post_load
    def datetime_to_utc(self, data: Dict[str, Any], **kwargs: Any):
        if data["until"] is not None:
            data["until"] = data["until"].astimezone(timezone.utc)
        return data


def remove_expired_elements(
    lst: List[ConfigIgnoredElement],
) -> List[ConfigIgnoredElement]:
    expired: List[ConfigIgnoredElement] = []
    now = datetime.now(tz=timezone.utc)
    for idx in range(len(lst) - 1, -1, -1):
        ignored = lst[idx]
        if ignored.until and ignored.until <= now:
            lst.pop(idx)
            expired.insert(0, ignored)

    return expired


def report_expired_elements(expired_lst: List[ConfigIgnoredElement]) -> None:
    for element in expired_lst:
        display_warning(
            f"{element} has an expired 'until' "
            f"date ({element.until}), please update your configuration file."
        )


@marshmallow_dataclass.dataclass
class IaCConfigIgnoredPath(ConfigIgnoredElement):
    path: str

    def __init__(
        self,
        path: str,
        comment: Optional[str] = None,
        until: Optional[datetime] = None,
        *args: Any,
        **kwargs: Any,
    ):
        super().__init__(comment, until, *args, **kwargs)
        self.path = path

    def __str__(self):
        return f"Path {self.path}"

    @pre_load
    def convert_paths(self, in_data: Union[str, Dict[str, Any]], **kwargs: Any):
        return {"path": in_data} if isinstance(in_data, str) else in_data


@marshmallow_dataclass.dataclass
class IaCConfigIgnoredPolicy(ConfigIgnoredElement):
    policy: str = field(metadata={"validate": validate_policy_id})

    def __init__(
        self,
        policy: str,
        comment: Optional[str] = None,
        until: Optional[datetime] = None,
        *args: Any,
        **kwargs: Any,
    ):
        super().__init__(comment, until, *args, **kwargs)
        self.policy = policy

    def __str__(self):
        return f"Policy {self.policy}"

    @pre_load
    def convert_policies(self, in_data: Union[str, Dict[str, Any]], **kwargs: Any):
        return {"policy": in_data} if isinstance(in_data, str) else in_data


@marshmallow_dataclass.dataclass
class IaCConfig(FilteredConfig):
    """
    Holds the iac config as defined .gitguardian.yaml files
    (local and global).
    """

    ignored_paths: List[IaCConfigIgnoredPath] = field(default_factory=list)
    ignored_policies: List[IaCConfigIgnoredPolicy] = field(default_factory=list)
    minimum_severity: str = "LOW"

    # If we hit any outdated ignore rules when loading the configuration file, we
    # want to keep them in order to display additional details but we do not want
    # to store them in ignored rules because they will be used to scan.
    # Instead, we store them in these new computed fields. However we must mark them
    # as excluded, otherwise they would be serialized if the user runs a command to
    # write in the configuration file, which would be odd.
    outdated_ignored_paths: List[IaCConfigIgnoredPath] = field(default_factory=list)
    outdated_ignored_policies: List[IaCConfigIgnoredPolicy] = field(
        default_factory=list
    )

    @post_load
    def validate_ignored_paths(self, data: Dict[str, Any], **kwargs: Any):
        expired_lst = remove_expired_elements(data["ignored_paths"])
        report_expired_elements(expired_lst)
        data["outdated_ignored_paths"] = expired_lst
        return data

    @post_load
    def validate_ignored_policies(self, data: Dict[str, Any], **kwargs: Any):
        expired_lst = remove_expired_elements(data["ignored_policies"])
        report_expired_elements(expired_lst)
        data["outdated_ignored_policies"] = expired_lst
        return data


def is_ghsa_valid(ghsa_id: str) -> bool:
    return bool(GHSA_ID_PATTERN.fullmatch(ghsa_id))


def validate_vuln_identifier(value: str):
    if not is_ghsa_valid(value):
        raise ValidationError(
            f"The given GHSA id '{value}' do not match the pattern '{GHSA_ID_PATTERN.pattern}'"
        )


@marshmallow_dataclass.dataclass
class SCAConfigIgnoredVulnerability(ConfigIgnoredElement):
    """
    A model of an ignored vulnerability for SCA. This allows to ignore all occurrences
    of a given vulnerability in a given dependency file.
    - identifier: identifier (currently: GHSA id) of the vulnerability to ignore
    - path: the path to the file in which ignore the vulnerability
    - comment: The ignored reason
    - until: A datetime until which the vulnerability is ignored
    """

    identifier: str = field(metadata={"validate": validate_vuln_identifier})
    path: str

    def __init__(
        self,
        identifier: str,
        path: str,
        comment: Optional[str] = None,
        until: Optional[datetime] = None,
        *args: Any,
        **kwargs: Any,
    ):
        super().__init__(comment, until, *args, **kwargs)
        self.identifier = identifier
        self.path = path

    def __str__(self):
        return f"Vulnerability {self.identifier}"


@marshmallow_dataclass.dataclass
class SCAConfig(FilteredConfig):
    """
    Holds the sca config as defined .gitguardian.yaml files
    (local and global).
    """

    ignored_paths: Set[str] = field(default_factory=set)
    minimum_severity: str = "LOW"
    ignored_vulnerabilities: List[SCAConfigIgnoredVulnerability] = field(
        default_factory=list
    )
    ignore_not_fixable: bool = False
    ignore_fixable: bool = False

    @post_load
    def validate_ignored_vulns(self, data: Dict[str, Any], **kwargs: Any):
        expired_lst = remove_expired_elements(data["ignored_vulnerabilities"])
        report_expired_elements(expired_lst)
        return data


@marshmallow_dataclass.dataclass
class UserConfig(FilteredConfig):
    """
    Holds all ggshield settings defined by the user in the .gitguardian.yaml files
    (local and global).
    """

    iac: IaCConfig = field(default_factory=IaCConfig)
    instance: Optional[str] = None
    exit_zero: bool = False
    verbose: bool = False
    allow_self_signed: bool = False
    max_commits_for_hook: int = 50
    sca: SCAConfig = field(default_factory=SCAConfig)
    secret: SecretConfig = field(default_factory=SecretConfig)
    debug: bool = False

    # If we hit any deprecated syntax when loading a configuration file, we do not
    # display them directly, otherwise the messages would also be shown when running
    # `ggshield config migrate`, which would be odd.
    # Instead, we store them in this list and a result_callback() function displays
    # them when we quit. When `config migrate` runs, it clears this list, so nothing
    # gets displayed.
    deprecation_messages: List[str] = field(default_factory=list)

    def save(self, config_path: Path) -> None:
        """
        Save config to config_path
        """
        dct = self.to_config_dict()
        save_yaml_dict(dct, config_path)

    def to_config_dict(self) -> Dict[str, Any]:
        dct = self.to_dict()
        default_dct = UserConfig.from_dict({}).to_dict()

        dct = remove_common_dict_items(dct, default_dct)

        dct["version"] = CURRENT_CONFIG_VERSION

        replace_in_keys(dct, old_char="_", new_char="-")
        return dct

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> Tuple["UserConfig", Path]:
        """
        Load the various user configs files to create a UserConfig object:
        - global user configuration file (in the home)
        - local user configuration file (in the repository)

        Returns a UserConfig instance, and the path where updates should be saved
        """
        deprecation_messages: List[str] = []
        if config_path:
            logger.debug("Loading custom config from %s", config_path)
            dct = UserConfig._load_config_dict(config_path, deprecation_messages)
            user_config = UserConfig.from_config_dict(dct)
            user_config.deprecation_messages = deprecation_messages
            return user_config, config_path

        user_config_dict: Dict[str, Any] = {}
        global_config_path = find_global_config_path()
        if global_config_path:
            dct = UserConfig._load_config_dict(global_config_path, deprecation_messages)
            update_dict_from_other(user_config_dict, dct)
            logger.debug("Loaded global config from %s", global_config_path)
        else:
            logger.debug("No global config")

        local_config_path = find_local_config_path()
        if local_config_path:
            dct = UserConfig._load_config_dict(local_config_path, deprecation_messages)
            update_dict_from_other(user_config_dict, dct)
            config_path = local_config_path
            logger.debug("Loaded local config from %s", local_config_path)
        else:
            logger.debug("No local config")

        user_config = UserConfig.from_config_dict(user_config_dict)
        user_config.deprecation_messages = deprecation_messages

        if config_path is None:
            config_path = Path(DEFAULT_LOCAL_CONFIG_PATH)
        return user_config, config_path

    @staticmethod
    def _load_config_dict(
        config_path: Path, deprecation_messages: List[str]
    ) -> Dict[str, Any]:
        """
        Load configuration from `config_path` as a dict.
        Appends any deprecation message regarding this file to `deprecation_messages`.
        """
        try:
            # load_yaml_dict returns None if `config_path` does not exist. In this case,
            # pretend we loaded a config file with just the version set to the latest
            # version so that we don't get deprecation messages
            dct = load_yaml_dict(config_path) or {"version": CURRENT_CONFIG_VERSION}
        except ValueError as exc:
            raise ParseError(str(exc)) from exc

        replace_in_keys(dct, old_char="_", new_char="-")

        config_version = dct.pop("version", 1)
        if config_version == 2:
            _fix_ignore_known_secrets(dct)
        elif config_version == 1:
            deprecation_messages.append(
                f"{config_path} uses a deprecated configuration file format."
                " Run `ggshield config migrate` to migrate it to the latest version."
            )
            dct = UserV1Config.load_v1_dict(dct, deprecation_messages)
        else:
            raise UnexpectedError(
                f"Don't know how to load config version {config_version}"
            )
        return dct

    @staticmethod
    def from_config_dict(data: Dict[str, Any]) -> "UserConfig":
        try:
            return UserConfig.from_dict(data)
        except ValidationError as exc:
            message = format_validation_error(exc)
            raise ParseError(message) from exc


UserConfig.SCHEMA = marshmallow_dataclass.class_schema(UserConfig)(
    exclude=(
        "deprecation_messages",
        "iac.outdated_ignored_paths",
        "iac.outdated_ignored_policies",
    )
)


def _fix_ignore_known_secrets(data: Dict[str, Any]) -> None:
    """Fix a mistake done when implementing ignore-known-secrets: since this is a secret
    specific key, it should have been stored in the "secret" mapping, not in the root
    one"""

    underscore_key = _IGNORE_KNOWN_SECRETS_KEY.replace("-", "_")

    for key in _IGNORE_KNOWN_SECRETS_KEY, underscore_key:
        value = data.pop(key, None)
        if value is not None:
            break
    else:
        # No key to fix
        return

    secret_dct = data.setdefault("secret", {})
    if _IGNORE_KNOWN_SECRETS_KEY in secret_dct or underscore_key in secret_dct:
        # Do not override if it's already there
        return
    secret_dct[_IGNORE_KNOWN_SECRETS_KEY] = value


@dataclass
class UserV1Config(FromDictMixin):
    """
    Can load a v1 .gitguardian.yaml file
    """

    instance: Optional[str] = None
    all_policies: bool = False
    exit_zero: bool = False
    matches_ignore: List[Dict[str, Optional[str]]] = field(default_factory=list)
    paths_ignore: Set[str] = field(default_factory=set)
    verbose: bool = False
    allow_self_signed: bool = False
    max_commits_for_hook: int = 50
    ignore_default_excludes: bool = False
    show_secrets: bool = False
    banlisted_detectors: Set[str] = field(default_factory=set)

    @staticmethod
    def load_v1(data: Dict[str, Any]) -> UserConfig:
        """
        Takes a dict representing a v1 .gitguardian.yaml and returns a v2 config object
        """
        # If data contains the old "api-url" key, turn it into an "instance" key,
        # but only if there is no "instance" key
        try:
            api_url = data.pop("api_url")
        except KeyError:
            pass
        else:
            if "instance" not in data:
                data["instance"] = api_to_dashboard_url(api_url, warn=True)

        UserV1Config.matches_ignore_to_dict(data)

        v1config = UserV1Config.from_dict(data)

        deprecation_messages = []

        if v1config.all_policies:
            deprecation_messages.append(
                "The `all_policies` option has been deprecated and is now ignored."
            )

        if v1config.ignore_default_excludes:
            deprecation_messages.append(
                "The `ignore_default_exclude` option has been deprecated and is now ignored."
            )

        ignored_matches = [
            IgnoredMatch.from_dict(secret) for secret in v1config.matches_ignore
        ]
        secret = SecretConfig(
            show_secrets=v1config.show_secrets,
            ignored_detectors=v1config.banlisted_detectors,
            ignored_matches=ignored_matches,
            ignored_paths=v1config.paths_ignore,
        )

        return UserConfig(
            instance=v1config.instance,
            exit_zero=v1config.exit_zero,
            verbose=v1config.verbose,
            allow_self_signed=v1config.allow_self_signed,
            max_commits_for_hook=v1config.max_commits_for_hook,
            secret=secret,
            deprecation_messages=deprecation_messages,
        )

    @staticmethod
    def load_v1_dict(
        data: Dict[str, Any], deprecation_messages: List[str]
    ) -> Dict[str, Any]:
        """
        Takes a dict representing a v1 .gitguardian.yaml and returns a v2 dict
        """
        # If data contains the old "api-url" key, turn it into an "instance" key,
        # but only if there is no "instance" key
        try:
            api_url = data.pop("api-url")
        except KeyError:
            pass
        else:
            if "instance" not in data:
                data["instance"] = api_to_dashboard_url(api_url, warn=True)

        UserV1Config.matches_ignore_to_dict(data)

        if "all-policies" in data:
            deprecation_messages.append(
                "The `all-policies` option has been deprecated and is now ignored."
            )

        if "ignore-default-excludes" in data:
            deprecation_messages.append(
                "The `ignore-default-exclude` option has been deprecated and is now ignored."
            )

        secret_dct = {}
        if matches_ignore := data.get("matches-ignore"):
            secret_dct["ignored-matches"] = [
                IgnoredMatch.from_dict(secret).to_dict() for secret in matches_ignore
            ]
            replace_in_keys(secret_dct["ignored-matches"], "_", "-")

        def copy_if_set(dct, dst_key, src_key=None):
            if src_key is None:
                src_key = dst_key
            try:
                value = data[src_key]
            except KeyError:
                return
            dct[dst_key] = value

        copy_if_set(secret_dct, "show-secrets")
        copy_if_set(secret_dct, "ignored-detectors", "banlisted-detectors")
        copy_if_set(secret_dct, "ignored-paths", "paths-ignore")

        dct = {
            "secret": secret_dct,
        }
        copy_if_set(dct, "instance")
        copy_if_set(dct, "exit-zero")
        copy_if_set(dct, "verbose")
        copy_if_set(dct, "allow-self-signed")
        copy_if_set(dct, "max-commits-for-hook")

        return dct

    @staticmethod
    def matches_ignore_to_dict(data: Dict[str, Any]) -> None:
        """
        v1 config format allowed to use just a hash of the secret for matches_ignore
        field v2 does not. This function converts the hash-only matches.
        """
        matches_ignore = data.get("matches_ignore")
        if not matches_ignore:
            return

        for idx, match in enumerate(matches_ignore):
            if isinstance(match, str):
                matches_ignore[idx] = {"name": "", "match": match}


UserV1Config.SCHEMA = marshmallow_dataclass.class_schema(UserV1Config)()
