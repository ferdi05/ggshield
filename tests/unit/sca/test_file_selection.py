import inspect
import io
import tarfile
from pathlib import Path
from typing import Set

import pytest
from pygitguardian import GGClient

from ggshield.core.utils import Filemode
from ggshield.sca.client import SCAClient
from ggshield.sca.file_selection import (
    get_all_files_from_sca_paths,
    is_not_excluded_from_sca,
    tar_sca_files_from_git_repo,
)
from ggshield.scan import StringScannable
from tests.repository import Repository
from tests.unit.conftest import my_vcr, write_text


FILE_NAMES = [
    "file1.txt",
    "file2.py",
    ".venv/dockerfile.txt",
    "foo/node_modules/file3.json",
    "foo/bar/file4.json",
]


@pytest.mark.parametrize(
    ("filepath", "expected_result"),
    [
        ("Pipfile", True),
        ("foo/.venv/Pipfile", False),
        ("foo/bar/.venvsomething", True),
    ],
)
def test_is_excluded_from_sca(filepath: str, expected_result: bool):
    """
    GIVEN a StringScannable
    WHEN calling is_excluded_from_sca
    THEN we get True if the file is inside a directory from SCA_IGNORE_LIST
    THEN we get False otherwise
    """
    scannable = StringScannable(filepath, "", Filemode.FILE)

    assert is_not_excluded_from_sca(scannable) is expected_result


def test_get_all_files_from_sca_paths(tmp_path):
    """
    GIVEN a directory
    WHEN calling get_all_files_from_sca_paths
    THEN we get the ones that are not excluded by is_excluded_from_sca
    """
    tmp_paths = [str(tmp_path / filename) for filename in FILE_NAMES]
    for path in tmp_paths:
        write_text(filename=path, content="")

    files = get_all_files_from_sca_paths(tmp_path, set(), True)
    assert len(files) == 3
    assert Path(".venv/dockerfile.txt") not in [Path(filepath) for filepath in files]
    assert Path("file2.py") in [Path(filepath) for filepath in files]
    assert Path("foo/bar/file4.json") in [Path(filepath) for filepath in files]


@pytest.mark.parametrize(
    ("branch_name", "expected_files"),
    (
        ("branch_with_vuln", {"Pipfile", "Pipfile.lock"}),
        ("branch_without_lock", {"Pipfile"}),
        ("branch_without_sca", set()),
    ),
)
def test_tar_sca_files_from_git_repo(
    dummy_sca_repo: Repository,
    client: GGClient,
    branch_name: str,
    expected_files: Set[str],
):
    """
    GIVEN a git repo and a ref
    WHEN calling tar_sca_files_from_git_repo for this repo and ref
    THEN we have the expected filenames in the tar
    """

    fun_name = inspect.currentframe().f_code.co_name
    with my_vcr.use_cassette(f"{fun_name}_{branch_name}"):
        sca_client = SCAClient(client)
        tar_bytes = tar_sca_files_from_git_repo(
            client=sca_client, directory=dummy_sca_repo.path, ref=branch_name
        )
        tar_obj = tarfile.open(fileobj=io.BytesIO(tar_bytes))
        assert set(tar_obj.getnames()) == expected_files


@my_vcr.use_cassette()
def test_tar_sca_files_from_git_repo_with_staged_files(
    dummy_sca_repo: Repository, client: GGClient
):
    """
    GIVEN a git repo and a ref
    WHEN calling tar_sca_files_from_git_repo for this repo and empty string as ref
    THEN we have the staged files in the tar
    """

    sca_client = SCAClient(client)
    dummy_sca_repo.git("checkout", "branch_without_sca")
    (dummy_sca_repo.path / "package.json").touch()
    dummy_sca_repo.add("package.json")
    tar_bytes = tar_sca_files_from_git_repo(
        client=sca_client, directory=dummy_sca_repo.path, ref=""
    )
    tar_obj = tarfile.open(fileobj=io.BytesIO(tar_bytes))
    assert set(tar_obj.getnames()) == {"package.json"}
