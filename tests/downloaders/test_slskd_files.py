"""Taking finished Soulseek downloads out of slskd's folder (``files.take_slskd_download``):
only Releasomat's own downloads (``releasomat/<id>/``), only plain files, never overwriting
(ADR 0003)."""

import os

import pytest

from app import files
from app.files import SLSKD_FOLDER, UnsafePath, take_slskd_download


@pytest.fixture
def slskd(tmp_path):
    """slskd's download folder: the owner's own downloads, and one of Releasomat's."""
    root = tmp_path / "slskd"
    (root / "Owner's Album").mkdir(parents=True)
    (root / "Owner's Album" / "01 Mine.flac").write_bytes(b"owner's download")
    ours = root / SLSKD_FOLDER / "batch-1"
    ours.mkdir(parents=True)
    (ours / "08 - Get Lucky.flac").write_bytes(b"fLaC our download")
    return root


@pytest.fixture
def staging(tmp_path):
    return files.new_staging_dir(tmp_path / "config")


def test_moves_releasomats_download_into_staging(slskd, staging):
    source = slskd / SLSKD_FOLDER / "batch-1" / "08 - Get Lucky.flac"
    moved = take_slskd_download(slskd, source, staging)
    assert moved == staging / "08 - Get Lucky.flac"
    assert moved.read_bytes() == b"fLaC our download"
    assert not source.exists()
    assert not (slskd / SLSKD_FOLDER / "batch-1").exists()  # its emptied folder is gone
    assert (slskd / SLSKD_FOLDER).is_dir()  # Releasomat's folder stays for the next ones
    assert (slskd / "Owner's Album" / "01 Mine.flac").read_bytes() == b"owner's download"


def test_relative_paths_count_from_releasomats_folder(slskd, staging):
    moved = take_slskd_download(slskd, "batch-1/08 - Get Lucky.flac", staging)
    assert moved.name == "08 - Get Lucky.flac"


def test_never_overwrites_in_staging(slskd, staging):
    (staging / "08 - Get Lucky.flac").write_bytes(b"already here")
    moved = take_slskd_download(
        slskd, slskd / SLSKD_FOLDER / "batch-1/08 - Get Lucky.flac", staging
    )
    assert moved.name == "08 - Get Lucky (2).flac"
    assert (staging / "08 - Get Lucky.flac").read_bytes() == b"already here"


def test_a_folder_with_other_files_stays(slskd, staging):
    folder = slskd / SLSKD_FOLDER / "batch-1"
    (folder / "wrong.flac").write_bytes(b"a wrong file the owner may want to look at")
    take_slskd_download(slskd, folder / "08 - Get Lucky.flac", staging)
    assert (folder / "wrong.flac").exists()


@pytest.mark.parametrize(
    "path",
    [
        "Owner's Album/01 Mine.flac",  # relative to releasomat/: doesn't exist there
        "../Owner's Album/01 Mine.flac",
        "batch-1/../../Owner's Album/01 Mine.flac",
    ],
)
def test_never_takes_the_owners_downloads(slskd, staging, path):
    with pytest.raises(UnsafePath):
        take_slskd_download(slskd, path, staging)
    with pytest.raises(UnsafePath):
        take_slskd_download(slskd, slskd / "Owner's Album" / "01 Mine.flac", staging)
    assert (slskd / "Owner's Album" / "01 Mine.flac").exists()
    assert list(staging.iterdir()) == []


def test_never_follows_symlinks(slskd, staging, tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    (music / "precious.flac").write_bytes(b"the owner's library")
    ours = slskd / SLSKD_FOLDER / "batch-1"
    (ours / "link.flac").symlink_to(music / "precious.flac")
    (slskd / SLSKD_FOLDER / "linked-folder").symlink_to(music)
    (ours / "inner-link.flac").symlink_to(ours / "08 - Get Lucky.flac")
    for path in (
        ours / "link.flac",
        slskd / SLSKD_FOLDER / "linked-folder" / "precious.flac",
        ours / "inner-link.flac",
        ours,
    ):
        with pytest.raises(UnsafePath):
            take_slskd_download(slskd, path, staging)
    assert (music / "precious.flac").read_bytes() == b"the owner's library"
    assert (ours / "08 - Get Lucky.flac").exists()
    assert list(staging.iterdir()) == []


def test_releasomats_folder_must_not_lead_elsewhere(tmp_path, staging):
    slskd = tmp_path / "slskd"
    slskd.mkdir()
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "batch").mkdir(parents=True)
    (elsewhere / "batch" / "x.flac").write_bytes(b"not slskd's")
    (slskd / SLSKD_FOLDER).symlink_to(elsewhere)
    with pytest.raises(UnsafePath):
        take_slskd_download(slskd, slskd / SLSKD_FOLDER / "batch" / "x.flac", staging)
    assert (elsewhere / "batch" / "x.flac").exists()


def test_missing_folder_is_refused(tmp_path, staging):
    with pytest.raises(UnsafePath):
        take_slskd_download(tmp_path / "not-mounted", "batch/x.flac", staging)


@pytest.mark.skipif(os.geteuid() == 0, reason="root may write anywhere")
def test_a_folder_releasomat_may_not_change_is_refused_before_copying(slskd, staging):
    folder = slskd / SLSKD_FOLDER / "batch-1"
    folder.chmod(0o555)
    try:
        with pytest.raises(PermissionError, match="PUID/PGID"):
            take_slskd_download(slskd, folder / "08 - Get Lucky.flac", staging)
        assert list(staging.iterdir()) == []
        assert (folder / "08 - Get Lucky.flac").exists()
    finally:
        folder.chmod(0o755)
