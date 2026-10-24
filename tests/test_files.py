"""app/files.py: Releasomat only touches its own files (ADR 0003). Every rule, every way out."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlmodel import Session

from app.files import (
    DeleteRefused,
    UnsafePath,
    clean_part,
    clear_staging,
    delete_by_owner,
    discard_staged,
    free_name,
    inside,
    new_staging_dir,
    render_template,
    save_download,
)
from app.models import Playlist, PlaylistTrack, Track, TrackFile

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def music(tmp_path):
    folder = tmp_path / "music"
    folder.mkdir(exist_ok=True)  # the config fixture may have made it already
    return folder


@pytest.fixture
def outside(tmp_path):
    """The rest of the library (or anything else): must never be touched."""
    folder = tmp_path / "library"
    folder.mkdir()
    (folder / "precious.flac").write_bytes(b"owner's music")
    return folder


# --- Staying inside -----------------------------------------------------------------------


def test_paths_inside_are_fine(music):
    assert (
        inside(music, "Artist/Artist - Title.mp3") == music.resolve() / "Artist/Artist - Title.mp3"
    )
    assert inside(music, music / "a.mp3") == music.resolve() / "a.mp3"  # absolute, but inside


@pytest.mark.parametrize(
    "path",
    [
        "../library/precious.flac",
        "Artist/../../library/precious.flac",
        "Artist/..",
        "..",
        "/etc/passwd",
        "",
        ".",
        "a\x00b",
    ],
)
def test_escape_attempts_are_refused(music, outside, path):
    with pytest.raises(UnsafePath):
        inside(music, path)


def test_absolute_path_outside_is_refused(music, outside):
    with pytest.raises(UnsafePath):
        inside(music, outside / "precious.flac")


def test_symlinked_folder_pointing_out_is_refused(music, outside):
    (music / "sneaky").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafePath):
        inside(music, "sneaky/precious.flac")
    with pytest.raises(UnsafePath):
        inside(music, "sneaky/new.mp3")


def test_symlinked_file_pointing_out_is_refused(music, outside):
    (music / "link.flac").symlink_to(outside / "precious.flac")
    with pytest.raises(UnsafePath):
        inside(music, "link.flac")


def test_missing_music_folder_is_refused(tmp_path):
    with pytest.raises(UnsafePath, match="doesn't exist"):
        inside(tmp_path / "not-mounted", "a.mp3")


# --- File names ---------------------------------------------------------------------------


def test_path_template():
    template = "{artist}/{artist} - {title}.{ext}"
    assert render_template(
        template, artist="Daft Punk", title="One More Time", album=None, ext="MP3"
    ) == ("Daft Punk/Daft Punk - One More Time.mp3")
    assert render_template(template, artist="AC/DC", title="T.N.T.", album=None, ext="flac") == (
        "AC_DC/AC_DC - T.N.T.flac"
    )
    assert render_template(template, artist="..", title="../../x", album=None, ext="mp3") == (
        "Unknown/Unknown - _.._x.mp3"
    )
    assert render_template("{album}/{title}.{ext}", artist="A", title="", album="", ext="") == (
        "Unknown/Unknown.bin"
    )
    with pytest.raises(ValueError, match="Unknown placeholder"):
        render_template("{genre}/{title}.{ext}", artist="A", title="T", album=None, ext="mp3")
    with pytest.raises(ValueError, match="empty"):
        render_template("../{title}.{ext}", artist="A", title="T", album=None, ext="mp3")


def test_clean_part():
    assert clean_part('What? "Yes": <No> | *Maybe*\\') == "What_ _Yes__ _No_ _ _Maybe__"
    assert clean_part(".hidden. ") == "hidden"
    assert clean_part("tab\there\nnew") == "tabherenew"  # control characters are dropped
    assert len(clean_part("x" * 500)) == 120
    assert clean_part(None) == "Unknown"


# --- Never overwrite ----------------------------------------------------------------------


def test_free_name_adds_a_number(music):
    target = music / "Song.mp3"
    assert free_name(target) == target
    target.write_bytes(b"1")
    assert free_name(target) == music / "Song (2).mp3"
    (music / "Song (2).mp3").write_bytes(b"2")
    assert free_name(target) == music / "Song (3).mp3"


def test_saving_never_overwrites(music, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    existing = music / "Artist" / "Artist - Song.mp3"
    existing.parent.mkdir()
    existing.write_bytes(b"the owner's copy")

    download = staging / "download.mp3"
    download.write_bytes(b"new download")
    saved = save_download(download, staging, music, "Artist/Artist - Song.mp3")
    assert saved == music.resolve() / "Artist" / "Artist - Song (2).mp3"
    assert saved.read_bytes() == b"new download"
    assert existing.read_bytes() == b"the owner's copy"
    assert not download.exists()  # moved, not copied

    second = staging / "again.mp3"
    second.write_bytes(b"another")
    assert save_download(second, staging, music, "Artist/Artist - Song.mp3").name == (
        "Artist - Song (3).mp3"
    )


def test_a_symlink_in_the_way_is_never_written_through(music, outside, tmp_path):
    (music / "Song.mp3").symlink_to(outside / "would-be-created.mp3")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "d.mp3").write_bytes(b"x")
    with pytest.raises(UnsafePath):
        save_download(staging / "d.mp3", staging, music, "Song.mp3")
    assert not (outside / "would-be-created.mp3").exists()
    assert (staging / "d.mp3").exists()


def test_saving_refuses_sources_outside_their_folder(music, outside, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    with pytest.raises(UnsafePath):
        save_download(outside / "precious.flac", staging, music, "x.flac")
    (staging / "link.flac").symlink_to(outside / "precious.flac")
    with pytest.raises(UnsafePath):
        save_download(staging / "link.flac", staging, music, "x.flac")
    assert (outside / "precious.flac").read_bytes() == b"owner's music"


def test_saving_refuses_targets_outside(music, outside, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "d.mp3").write_bytes(b"x")
    with pytest.raises(UnsafePath):
        save_download(staging / "d.mp3", staging, music, "../library/precious.flac")
    assert (staging / "d.mp3").exists()  # nothing happened


# --- Staging -----------------------------------------------------------------------------


def test_staging_folders_live_in_the_config_folder(tmp_path, music):
    config = tmp_path / "config"
    staging = new_staging_dir(config)
    assert staging.is_dir() and staging.parent == (config / "staging").resolve()
    (staging / "wrong.opus").write_bytes(b"x")
    discard_staged(staging, staging / "wrong.opus")
    assert not (staging / "wrong.opus").exists()
    (staging / "leftover.part").write_bytes(b"x")
    clear_staging(config, staging)
    assert not staging.exists()
    other = new_staging_dir(config)
    clear_staging(config)  # at startup: every leftover
    assert not other.exists() and (config / "staging").is_dir()


def test_staging_cleanup_never_leaves_the_staging_folder(tmp_path, music, outside):
    config = tmp_path / "config"
    staging = new_staging_dir(config)
    with pytest.raises(UnsafePath):
        discard_staged(staging, outside / "precious.flac")
    with pytest.raises(UnsafePath):
        clear_staging(config, outside)
    (config / "staging" / "sneaky").symlink_to(outside, target_is_directory=True)
    clear_staging(config)
    assert (outside / "precious.flac").exists()


# --- Never delete on its own; only the owner deletes --------------------------------------


def _downloaded(session, path="Artist/song.mp3", by_releasomat=True, playlists=()):
    track = Track(artist="A", title="Song")
    session.add(track)
    session.commit()
    file = TrackFile(track_id=track.id, path=path, downloaded_by_releasomat=by_releasomat)
    session.add(file)
    for number, (title, left) in enumerate(playlists):
        playlist = Playlist(source="deezer", source_id=f"p{number}", url="u", title=title)
        session.add(playlist)
        session.commit()
        removed = NOW if left else None
        session.add(PlaylistTrack(playlist_id=playlist.id, track_id=track.id, position=1,
                                  removed_at=removed))  # fmt: skip
    session.commit()
    return file


def test_the_owner_deletes_a_download_and_learns_which_playlists_have_it(engine, music):
    song = music / "Artist" / "song.mp3"
    song.parent.mkdir()
    song.write_bytes(b"x")
    with Session(engine) as session:
        file = _downloaded(session, playlists=[("Mine", False), ("Old", True), ("Also", False)])
        result = delete_by_owner(session, file, music)
    assert not song.exists() and not song.parent.exists()  # empty folders go too
    assert music.exists()  # never the music folder itself
    assert result.still_in == ["Also", "Mine"]  # the confirmation names them; "Old" left


def test_files_releasomat_did_not_download_are_never_deleted(engine, music):
    song = music / "song.mp3"
    song.write_bytes(b"x")
    with Session(engine) as session:
        with pytest.raises(DeleteRefused, match="didn't download"):
            delete_by_owner(session, _downloaded(session, "song.mp3", by_releasomat=False), music)
        with pytest.raises(DeleteRefused):  # a song that was in the library: no path
            delete_by_owner(session, _downloaded(session, None), music)
    assert song.exists()


def test_owner_delete_never_leaves_the_folder_or_follows_links(engine, music, outside):
    (music / "link.flac").symlink_to(outside / "precious.flac")
    (music / "inner.mp3").write_bytes(b"x")
    (music / "inner-link.mp3").symlink_to(music / "inner.mp3")
    (music / "folder").mkdir()
    with Session(engine) as session:
        with pytest.raises(UnsafePath):
            delete_by_owner(session, _downloaded(session, "../library/precious.flac"), music)
        with pytest.raises((UnsafePath, DeleteRefused)):
            delete_by_owner(session, _downloaded(session, "link.flac"), music)
        with pytest.raises(DeleteRefused, match="not a plain file"):
            delete_by_owner(session, _downloaded(session, "inner-link.mp3"), music)
        with pytest.raises(DeleteRefused, match="not a plain file"):
            delete_by_owner(session, _downloaded(session, "folder"), music)
    assert (outside / "precious.flac").exists() and (music / "inner.mp3").exists()


def test_nothing_but_the_owner_delete_function_deletes_music():
    """ADR 0003: no refresh, schedule, playlist change or setting deletes music. The only
    deleting function is ``delete_by_owner``, and nothing in the app calls it (yet: the delete
    button arrives in phase 4)."""
    app = Path(__file__).resolve().parent.parent / "app"
    for path in app.rglob("*.py"):
        if path.name != "files.py":
            assert "delete_by_owner" not in path.read_text(), path.relative_to(app)


def test_no_other_module_deletes_or_moves_files():
    """ADR 0003: only app/files.py creates, moves or deletes music files."""
    app = Path(__file__).resolve().parent.parent / "app"
    risky = ("unlink(", "rmdir(", "rmtree(", "os.remove(", "shutil.move(", "os.rename(", ".rename(")
    for path in app.rglob("*.py"):
        if path.name == "files.py" or "migrations" in path.parts:
            continue
        text = path.read_text()
        for word in risky:
            assert word not in text, f"{path.relative_to(app)} uses {word}"
