"""app/lifecycle.py: every state change of a downloaded file (ADR 0009), one test each, and the
conditional updates that keep two callers acting at the same moment from both changing a row.
Also finding a file by the path Tagwerk sends."""

import unicodedata

import pytest
from sqlmodel import Session

from app import lifecycle
from app.files import DeleteRefused
from app.lifecycle import AmbiguousPath, BadPath, Result
from app.models import Track, TrackFile

EPLE = "Röyksopp/Röyksopp - Eple.opus"
SONG = {"id": "lib1", "suffix": "flac", "bitRate": 900}  # its copy elsewhere in the library


def row(session: Session, **values) -> TrackFile:
    """A track and its file row; by default a download in Releasomat's folder."""
    track = Track(artist="Röyksopp", title="Eple")
    session.add(track)
    session.commit()
    defaults = {
        "status": "done",
        "path": EPLE,
        "downloaded_by_releasomat": True,
        "navidrome_song_id": "area1",
        "downloader": "youtube",
        "format": "opus",
        "details": {"origin": "https://y/1"},
    }
    file = TrackFile(track_id=track.id, **(defaults | values))
    session.add(file)
    session.commit()
    session.refresh(file)
    return file


def state(file: TrackFile) -> tuple:
    return (
        file.status,
        file.path,
        file.former_path,
        file.downloaded_by_releasomat,
        file.navidrome_song_id,
    )


def other_session(engine, file: TrackFile) -> tuple[Session, TrackFile]:
    """The same row, loaded by a second caller (another request, or a refresh)."""
    session = Session(engine)
    return session, session.get(TrackFile, file.id)


# --- Finding a file by its path -------------------------------------------------------------


def test_area_paths_accepts_the_contracts_spellings():
    nfd = unicodedata.normalize("NFD", EPLE)
    assert lifecycle.area_paths(EPLE, "Releasomat") == [EPLE]
    assert lifecycle.area_paths(nfd, "Releasomat") == [EPLE]  # compared in NFC
    assert lifecycle.area_paths(f"Releasomat/{EPLE}", "Releasomat") == [f"Releasomat/{EPLE}", EPLE]
    assert lifecycle.area_paths(f"/music/Releasomat/{EPLE}", "Releasomat") == [EPLE]
    assert lifecycle.area_paths(f"/mnt/user/Music/Releasomat/{EPLE}", "/Releasomat/") == [EPLE]
    assert lifecycle.area_paths(f"/music/Releasomat/{EPLE}", "Music/Releasomat") == []
    assert lifecycle.area_paths(f"/data/Music/Releasomat/{EPLE}", "Music/Releasomat") == [EPLE]
    assert lifecycle.area_paths(f"/music/Other/{EPLE}", "Releasomat") == []  # not the area
    assert lifecycle.area_paths("/music/Releasomat", "Releasomat") == []  # the folder itself


@pytest.mark.parametrize(
    "path",
    ["", "\x00", "a\x00b.mp3", "../x.mp3", "A/../../x.mp3", "./A/x.mp3", "A//x.mp3", "A/", "/"],
)
def test_area_paths_refuses_what_isnt_a_plain_path(path):
    with pytest.raises(BadPath):
        lifecycle.area_paths(path, "Releasomat")


def test_find_by_path_matches_path_and_former_path(session):
    here = row(session)
    gone = row(session, status="adopted", path=None, former_path="A/A - Gone.opus")
    library = row(session, status="in_library", path=None, downloaded_by_releasomat=False)
    nfd = unicodedata.normalize("NFD", EPLE)
    for raw in (EPLE, nfd, f"Releasomat/{EPLE}", f"/music/Releasomat/{nfd}"):
        assert lifecycle.find_by_path(session, raw, "Releasomat").id == here.id, raw
    assert lifecycle.find_by_path(session, "A/A - Gone.opus", "Releasomat").id == gone.id
    assert lifecycle.find_by_path(session, "A/A - Other.opus", "Releasomat") is None
    assert lifecycle.find_by_path(session, "/elsewhere/x.opus", "Releasomat") is None
    assert library.path is None  # songs that were in the library have no path: never found
    # With the id from an earlier answer, only that row counts.
    assert lifecycle.find_by_path(session, EPLE, "Releasomat", file_id=here.id).id == here.id
    assert lifecycle.find_by_path(session, EPLE, "Releasomat", file_id=gone.id) is None
    with pytest.raises(BadPath):
        lifecycle.find_by_path(session, "../etc/passwd", "Releasomat")


def test_find_by_path_matches_stored_paths_in_any_unicode_form(session):
    stored = row(session, path=unicodedata.normalize("NFD", EPLE))  # as a service spelled it
    assert lifecycle.find_by_path(session, EPLE, "Releasomat").id == stored.id


def test_find_by_path_never_guesses(session):
    first = row(session, status="adopted", path=None, former_path=EPLE)
    second = row(session)  # a new download got the same name
    with pytest.raises(AmbiguousPath) as error:
        lifecycle.find_by_path(session, EPLE, "Releasomat")
    assert error.value.file_ids == [first.id, second.id]
    assert lifecycle.find_by_path(session, EPLE, "Releasomat", file_id=second.id).id == second.id
    # "Releasomat/x" may mean the file "Releasomat/x" or "x" inside the area: both exist.
    row(session, path="Releasomat/Releasomat - Song.opus")
    row(session, path="Releasomat - Song.opus")
    with pytest.raises(AmbiguousPath):
        lifecycle.find_by_path(session, "Releasomat/Releasomat - Song.opus", "Releasomat")


# --- adopt ------------------------------------------------------------------------------------


def test_adopt(session):
    file = row(session)
    assert lifecycle.adopt(session, file) == Result(True, True)
    assert state(file) == ("adopted", None, EPLE, False, None)
    assert file.adopted_at is not None and file.details["old_song_id"] == "area1"
    assert file.details["origin"] == "https://y/1"  # the origin stays in the database
    assert lifecycle.adopt(session, file) == Result(True)  # repeated: ok, nothing changes


def test_adopt_after_a_refresh_guessed_the_file_was_gone(session):
    file = row(session, status="disliked", path=None, former_path=EPLE)
    file.downloaded_by_releasomat, file.disliked_reason = False, "vanished"
    session.commit()
    assert lifecycle.adopt(session, file) == Result(True, True)
    assert (file.status, file.disliked_reason, file.disliked_at) == ("adopted", None, None)
    assert file.adopted_at is not None


def test_adopt_of_a_file_already_found_in_the_library(session):
    file = row(session, status="in_library", path=None, former_path=EPLE, navidrome_song_id="lib1")
    file.downloaded_by_releasomat, file.details = False, {"moved": True}
    session.commit()
    assert lifecycle.adopt(session, file) == Result(True, True)
    assert file.status == "in_library" and file.adopted_at is not None  # stays linked
    assert lifecycle.adopt(session, file) == Result(True)


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        ({"status": "disliked", "disliked_reason": "deleted"}, "Don't like"),
        ({"status": "pending", "path": None, "downloaded_by_releasomat": False}, "no downloaded"),
        ({"status": "in_library", "path": None, "downloaded_by_releasomat": False}, "no download"),
    ],
)
def test_adopt_refuses_what_isnt_a_download_in_the_folder(session, values, reason):
    file = row(session, **values)
    before = state(file)
    result = lifecycle.adopt(session, file)
    assert not result.ok and not result.changed and reason in result.reason
    assert state(file) == before


def test_adopt_and_a_refresh_at_the_same_moment(engine, session):
    """Both decided on "done at this path"; only the first change happens, the other one looks
    again and answers from the new state."""
    file = row(session)
    other, same = other_session(engine, file)
    with other:
        assert lifecycle.left_folder(other, same, EPLE) == Result(True, True)  # refresh first
    result = lifecycle.adopt(session, file)  # the late notice still counts (vanished)
    assert result == Result(True, True) and file.status == "adopted"

    file = row(session)
    other, same = other_session(engine, file)
    assert lifecycle.adopt(session, file).changed  # the notice first
    with other:
        result = lifecycle.left_folder(other, same, EPLE)
        assert not result.ok and not result.changed  # the refresh's decision is dropped
        assert same.status == "adopted"


# --- returned ---------------------------------------------------------------------------------


def test_returned_after_a_failed_move(session):
    file = row(session)
    lifecycle.adopt(session, file)
    assert lifecycle.returned(session, file, "move_failed") == Result(True, True)
    assert state(file) == ("done", EPLE, None, True, "area1")  # exactly as before adopt
    assert file.adopted_at is None and file.details["returned"] == "move_failed"
    assert lifecycle.returned(session, file, "move_failed") == Result(True)  # repeated


def test_returned_given_back_is_found_again_after_a_scan(session):
    file = row(session)
    lifecycle.adopt(session, file)
    lifecycle.link_library_copy(session, file, SONG, matched_by="mbid")
    assert lifecycle.returned(session, file, "given_back") == Result(True, True)
    assert state(file) == ("done", EPLE, None, True, None)  # Navidrome's ID: after the scan


def test_returned_refusals(session):
    disliked = row(session, status="disliked", path=None, former_path=EPLE)
    disliked.disliked_reason = "deleted"
    session.commit()
    result = lifecycle.returned(session, disliked, "given_back")
    assert not result.ok and "Don't like" in result.reason
    never = row(session, status="pending", path=None, downloaded_by_releasomat=False)
    assert lifecycle.returned(session, never, "move_failed").reason == lifecycle.NOT_ADOPTED
    with pytest.raises(ValueError):
        lifecycle.returned(session, never, "lost")


# --- discard and restore ----------------------------------------------------------------------


def test_discard_and_restore(session):
    file = row(session)
    lifecycle.adopt(session, file)
    assert lifecycle.discard(session, file) == Result(True, True)
    assert (file.status, file.disliked_reason) == ("disliked", "discarded")
    assert file.disliked_at is not None and file.adopted_at is not None
    assert lifecycle.discard(session, file) == Result(True)  # repeated
    assert lifecycle.restore(session, file) == Result(True, True)
    assert (file.status, file.disliked_reason, file.disliked_at) == ("adopted", None, None)
    assert lifecycle.restore(session, file) == Result(True)  # repeated


def test_discard_and_restore_of_a_linked_track_keep_it_linked(session):
    file = row(session)
    lifecycle.adopt(session, file)
    lifecycle.link_library_copy(session, file, SONG, matched_by="name")
    assert lifecycle.discard(session, file) == Result(True, True)
    assert (file.status, file.navidrome_song_id, file.disliked_reason) == (
        "in_library", "lib1", "discarded"
    )  # fmt: skip
    assert lifecycle.restore(session, file) == Result(True, True)
    assert (file.status, file.disliked_reason) == ("in_library", None)


def test_discard_and_restore_refusals(session):
    file = row(session)  # never adopted
    assert lifecycle.discard(session, file).reason == lifecycle.NOT_ADOPTED
    assert not lifecycle.restore(session, file).ok
    deleted = row(session, status="disliked", path=None, former_path=EPLE)
    deleted.disliked_reason = "deleted"
    session.commit()
    assert "Don't like" in lifecycle.restore(session, deleted).reason
    assert state(file)[0] == "done" and deleted.status == "disliked"


def test_a_discard_replaces_a_guessed_dont_like(session):
    file = row(session, status="disliked", path=None, former_path=EPLE)
    file.disliked_reason = "vanished"
    session.commit()
    assert lifecycle.discard(session, file) == Result(True, True)
    assert file.disliked_reason == "discarded"


# --- mark_disliked and undo_dislike -----------------------------------------------------------


def test_mark_disliked_after_the_owner_deleted_the_file(session):
    file = row(session)
    assert lifecycle.mark_disliked(session, file, "deleted") == Result(True, True)
    assert state(file) == ("disliked", None, EPLE, False, None)
    assert (file.disliked_reason, file.details["old_song_id"]) == ("deleted", "area1")
    assert lifecycle.mark_disliked(session, file, "owner") == Result(True)  # already


def test_mark_disliked_of_a_missing_track(session):
    file = row(session, status="failed", path=None, downloaded_by_releasomat=False)
    file.navidrome_song_id = None
    session.commit()
    assert lifecycle.mark_disliked(session, file, "owner") == Result(True, True)
    assert (file.status, file.former_path, file.disliked_reason) == ("disliked", None, "owner")


def test_mark_disliked_never_leaves_a_file_behind(session):
    file = row(session)
    result = lifecycle.mark_disliked(session, file, "owner")
    assert not result.ok and "delete it" in result.reason
    assert state(file) == ("done", EPLE, None, True, "area1")
    with pytest.raises(ValueError):
        lifecycle.mark_disliked(session, file, "bored")


def test_the_owners_word_replaces_a_guessed_dont_like(session):
    file = row(session)
    lifecycle.left_folder(session, file, EPLE)
    assert lifecycle.mark_disliked(session, file, "deleted") == Result(True, True)
    assert file.disliked_reason == "deleted"


def test_undo_dislike(session):
    file = row(session)
    lifecycle.adopt(session, file)
    lifecycle.discard(session, file)
    assert lifecycle.undo_dislike(session, file) == Result(True, True)
    assert (file.status, file.disliked_at, file.disliked_reason, file.adopted_at) == (
        "pending", None, None, None
    )  # fmt: skip
    assert lifecycle.undo_dislike(session, file) == Result(True)  # repeated


def test_undo_dislike_of_a_linked_track_keeps_it_linked(session):
    file = row(session)
    lifecycle.mark_disliked(session, file, "deleted")
    lifecycle.link_library_copy(session, file, SONG, matched_by="isrc")
    assert lifecycle.undo_dislike(session, file) == Result(True, True)
    assert (file.status, file.navidrome_song_id, file.disliked_at) == ("in_library", "lib1", None)


# --- left_folder, link_library_copy, status_after_song_gone -----------------------------------


def test_left_folder_found_in_the_library(session):
    file = row(session)
    result = lifecycle.left_folder(session, file, EPLE, SONG, matched_by="mbid", score=100.0)
    assert result == Result(True, True)
    assert state(file) == ("in_library", None, EPLE, False, "lib1")
    assert (file.format, file.bitrate) == ("flac", 900)
    assert file.details["moved"] is True and file.details["old_song_id"] == "area1"
    assert file.disliked_at is None and file.adopted_at is None


def test_left_folder_not_found_is_a_guessed_dont_like(session):
    file = row(session)
    assert lifecycle.left_folder(session, file, EPLE) == Result(True, True)
    assert state(file) == ("disliked", None, EPLE, False, None)
    assert file.disliked_reason == "vanished" and file.disliked_at is not None


def test_left_folder_only_acts_at_the_path_it_checked(session):
    file = row(session)
    result = lifecycle.left_folder(session, file, "Other/Other - Song.opus")
    assert not result.ok and state(file) == ("done", EPLE, None, True, "area1")


def test_link_library_copy(session):
    adopted = row(session)
    lifecycle.adopt(session, adopted)
    assert lifecycle.link_library_copy(session, adopted, SONG, matched_by="mbid").changed
    assert state(adopted) == ("in_library", None, EPLE, False, "lib1")
    assert adopted.adopted_at is not None  # it stays an adopted track

    guessed = row(session)
    lifecycle.left_folder(session, guessed, EPLE)
    assert lifecycle.link_library_copy(session, guessed, SONG).changed
    assert (guessed.status, guessed.disliked_at, guessed.disliked_reason) == (
        "in_library", None, None
    )  # fmt: skip
    assert guessed.details["moved"] is True  # the guess is lifted: the owner moved it

    deleted = row(session)
    lifecycle.mark_disliked(session, deleted, "deleted")
    assert lifecycle.link_library_copy(session, deleted, SONG).changed
    assert (deleted.status, deleted.disliked_reason) == ("in_library", "deleted")

    done = row(session)
    assert not lifecycle.link_library_copy(session, done, SONG).ok


def test_status_after_song_gone(session):
    adopted, disliked, plain = row(session), row(session), row(session, status="in_library")
    lifecycle.adopt(session, adopted)
    lifecycle.link_library_copy(session, adopted, SONG)
    lifecycle.mark_disliked(session, disliked, "deleted")
    lifecycle.link_library_copy(session, disliked, SONG)
    assert lifecycle.status_after_song_gone(adopted) == "adopted"
    assert lifecycle.status_after_song_gone(disliked) == "disliked"
    assert lifecycle.status_after_song_gone(plain) == "pending"
    assert lifecycle.KEPT_AWAY == ("adopted", "disliked")


# --- delete_and_dislike: the owner's delete ---------------------------------------------------


def test_delete_and_dislike(session, config):
    song = config.music_dir / EPLE
    song.parent.mkdir(parents=True)
    song.write_bytes(b"music")
    (config.music_dir / "Other.opus").write_bytes(b"other")
    file = row(session)
    deleted, result = lifecycle.delete_and_dislike(session, file, config.music_dir)
    assert result == Result(True, True) and deleted.path == song.resolve()
    assert not song.exists() and (config.music_dir / "Other.opus").exists()
    assert (file.status, file.disliked_reason, file.former_path) == ("disliked", "deleted", EPLE)
    assert lifecycle.delete_and_dislike(session, file, config.music_dir) == (None, Result(True))


def test_delete_and_dislike_refuses_what_releasomat_didnt_download(session, config):
    song = config.music_dir / "Lib.opus"
    song.write_bytes(b"music")
    file = row(session, path="Lib.opus", downloaded_by_releasomat=False, status="in_library")
    with pytest.raises(DeleteRefused):
        lifecycle.delete_and_dislike(session, file, config.music_dir)
    assert song.exists() and file.status == "in_library"
    adopted = row(session)
    lifecycle.adopt(session, adopted)
    with pytest.raises(DeleteRefused):  # Tagwerk has it: nothing to delete here
        lifecycle.delete_and_dislike(session, adopted, config.music_dir)
    assert adopted.status == "adopted"
