#!/usr/bin/env python3
import argparse
import configparser
import importlib
import pkgutil
from collections.abc import ItemsView
from typing import Literal, cast

from typing_extensions import NotRequired, TypedDict


AlertingLevel = Literal["error", "warn", "none"]
ValueType = AlertingLevel | str | list[str]


class Contract(TypedDict):
    name: str
    type: Literal["layers", "independence", "forbidden"]
    layers: NotRequired[list[str]]
    modules: NotRequired[list[str]]
    ignore_imports: NotRequired[list[str]]
    unmatched_ignore_imports_alerting: NotRequired[AlertingLevel]
    source_modules: NotRequired[list[str]]
    forbidden_modules: NotRequired[list[str]]


NOTICE = (
    "# This file has been generated by ./scripts/generate-import-linter-config.py,"
    " do not edit by hand!"
)
CONTRACTS = "importlinter:contracts"
STATIC_CONFIG = {
    "importlinter": {
        "root_package": "ggshield",
        "include_external_packages": True,
    },
    CONTRACTS: [
        {
            "name": "ggshield-layers",
            "type": "layers",
            "layers": [
                "ggshield.cmd|%",
                "ggshield.verticals|%",
                "ggshield.core",
                "ggshield.utils | pygitguardian",
            ],
            "ignore_imports": [
                "ggshield.cmd.main -> ggshield.cmd.**",
                "ggshield.cmd.** -> ggshield.cmd.utils.*",
            ],
            "unmatched_ignore_imports_alerting": "warn",
        },
        {
            "name": "verticals-cmd-transversals",
            "type": "forbidden",
            "source_modules": [
                "ggshield.cmd.%",
            ],
            "forbidden_modules": [
                "ggshield.verticals.%",
            ],
            "ignore_imports": [
                "ggshield.cmd.{}.** -> ggshield.verticals.{}",
                "ggshield.cmd.{}.** -> ggshield.verticals.{}.**",
                # FIXME: #521 - enforce boundaries between cmd.auth and verticals.hmsl
                "ggshield.cmd.auth.** -> ggshield.verticals.hmsl.**",
            ],
            "unmatched_ignore_imports_alerting": "none",
        },
    ],
}


def get_submodules(*, name: str, prefixed: bool) -> list[str]:
    """Retrieve the modules included in a package"""
    module = importlib.import_module(name)
    return [
        x.name
        for x in pkgutil.iter_modules(
            path=module.__path__,
            prefix=f"{module.__name__}." if prefixed else "",
        )
    ]


def expand_glob(line: str) -> list[str]:
    """
    Expand the *glob*

    supported globs:
    - "xxx.%" => ["xxx.sub_module_a", "xxx.sub_module_b"]
    - "xxx|%" => ["xxx.sub_module_a | xxx.sub_module_b"]
    - "xxx.{} -> yyy.{}" => [
        "xxx.sub_module_a -> yyy.sub_module_a",
        "xxx.sub_module_b -> yyy.sub_module_b"
      ]
    - "xxx" (no globs) => ["xxx"]
    """
    if line.endswith(".%"):
        return sorted(get_submodules(name=line[:-2], prefixed=True))
    if line.endswith("|%"):
        return [" | ".join(sorted(get_submodules(name=line[:-2], prefixed=True)))]
    if "{}" in line:
        name = line[: line.index(".{}")]
        return [
            line.replace("{}", module)
            for module in get_submodules(name=name, prefixed=False)
        ]
    return [line]


def expand_modules(
    *,
    values: list[str],
    ordered: bool = False,
) -> list[str]:
    """Build the list with the glob expanded"""
    expanded = (
        module_name
        for module_name_or_glob in values
        for module_name in expand_glob(module_name_or_glob)
    )
    return sorted(expanded) if ordered else list(expanded)


def expand_value(value: ValueType, key: str) -> ValueType:
    """Build the list with items expanded"""
    if isinstance(value, list):
        typed_value = cast(list[str], value)
        return expand_modules(
            values=typed_value,
            ordered=key != "layers",
        )

    return value


def normalize_value(data: ValueType) -> bool | str:
    """Normalize value to be compatible with import-linter config format"""
    if isinstance(data, list):
        return "\n".join(["", *data])
    return data


def normalize_contract(contract: Contract) -> tuple[str, dict[str, bool | str]]:
    """Normalize contract to be compatible with import-linter config format"""
    cid = compute_contract_id(contract["name"])
    content = {
        key: normalize_value(expand_value(value, key))
        for key, value in cast(ItemsView[str, ValueType], contract.items())
    }
    return cid, content


def compute_contract_id(name: str) -> str:
    """Compute an ID for the contract based on its name"""
    slug = name.lower().replace(" ", "-")
    return f"importlinter:contract:{slug}"


def normalize_contracts(config) -> dict[str, dict[str, bool | str]]:
    """Build contracts from template and expand the globs"""
    normalized = {key: value for key, value in config.items() if key != CONTRACTS}

    for contract in config[CONTRACTS]:
        cid, content = normalize_contract(contract)
        normalized[cid] = content

    return normalized


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=argparse.FileType("w"), nargs="?", default="-")
    args = parser.parse_args()

    config = configparser.ConfigParser()
    config.read_dict(normalize_contracts(STATIC_CONFIG))
    args.output.write(f"{NOTICE}\n\n")
    config.write(args.output)


if __name__ == "__main__":
    main()
