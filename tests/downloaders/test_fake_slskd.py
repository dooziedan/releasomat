"""The fake slskd (scripts/fake_slskd.py) answers like slskd 0.26.1: the same field names as the
saved answers in tests/fixtures/online/slskd_*.json, the same states, auth and limits. When the
owner allows recording real answers, these tests show where the fake differs from them."""

import asyncio
import json
import shutil
from pathlib import Path

import httpx
import pytest

from scripts.fake_slskd import EXTRA_SONGS, ffmpeg_audio, load_catalog

ANSWERS = Path(__file__).parent.parent / "fixtures" / "online"
SEARCH = "/api/v0/searches"
BATCHES = "/api/v0/transfers/downloads/batches"


def saved(name: str):
    return json.loads((ANSWERS / name).read_text())


@pytest.fixture
def client_for(api_key):
    def make(fake, key=api_key) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fake.app),
            base_url="http://slskd.test:5030",
            headers={"X-API-Key": key},
        )

    return make


async def wait_until(condition, seconds=3.0):
    for _ in range(int(seconds / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


async def finished_search(client, text="daft punk get lucky") -> str:
    started = await client.post(SEARCH, json={"id": None, "searchText": text})
    search_id = started.json()["id"]
    while not (await client.get(f"{SEARCH}/{search_id}")).json()["isComplete"]:
        await asyncio.sleep(0.01)
    return search_id


async def test_wrong_or_missing_key_gets_401(fake, client_for):
    async with client_for(fake, key="wrong") as client:
        answer = await client.get("/api/v0/application")
        assert answer.status_code == 401 and answer.content == b""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake.app), base_url="http://slskd.test"
    ) as client:
        assert (await client.get("/api/v0/searches")).status_code == 401
        assert (await client.get("/")).status_code == 200  # the web page needs no key


async def test_answers_have_slskds_field_names(fake, client_for):
    async with client_for(fake) as client:
        state = (await client.get("/api/v0/application")).json()
        assert set(state["version"]) == set(saved("slskd_application.json")["version"])
        assert set(state["server"]) == set(saved("slskd_application.json")["server"])
        started = await client.post(SEARCH, json={"searchText": "daft punk get lucky"})
        assert set(started.json()) == set(saved("slskd_search_started.json"))
        assert started.json()["state"] == "InProgress"
        search_id = started.json()["id"]
        # Like slskd: no responses until the search is complete.
        assert (await client.get(f"{SEARCH}/{search_id}/responses")).json() == []
        await wait_until(lambda: fake.searches[search_id].is_complete())
        done = (await client.get(f"{SEARCH}/{search_id}")).json()
        assert set(done) == set(saved("slskd_search_complete.json"))
        assert done["state"] == "Completed, TimedOut" and done["isComplete"]
        responses = (await client.get(f"{SEARCH}/{search_id}/responses")).json()
        real = saved("slskd_search_responses.json")
        assert {set(r) == set(real[0]) for r in responses} == {True}
        files = [f for r in responses for f in r["files"] + r["lockedFiles"]]
        assert {set(f) == set(real[0]["files"][0]) for f in files} == {True}
        assert all("\\" in f["filename"] for f in files)

        offer = next(r for r in responses if r["username"] == "flacfan")["files"][0]
        body = {
            "id": "0f6b2f3e-6c55-4b8e-9d2a-5d1f1e7c9a42",
            "username": "flacfan",
            "files": [{"filename": offer["filename"], "size": offer["size"]}],
            "options": {"destination": "releasomat/0f6b2f3e-6c55-4b8e-9d2a-5d1f1e7c9a42"},
        }
        created = await client.post(BATCHES, json=body)
        assert created.status_code == 201
        real_batch = saved("slskd_batch_created.json")
        assert set(created.json()) == set(real_batch)
        assert set(created.json()["batch"]) == set(real_batch["batch"])
        transfer = created.json()["batch"]["transfers"][0]
        assert set(transfer) == set(real_batch["batch"]["transfers"][0])
        await wait_until(lambda: fake.transfers[transfer["id"]].local_filename)
        batch = (await client.get(f"{BATCHES}/{body['id']}")).json()
        assert set(batch) == set(saved("slskd_batch_succeeded.json"))
        assert batch["transfers"][0]["state"] == "Completed, Succeeded"
        assert batch["transfers"][0]["percentComplete"] == 100
        one = (await client.get(f"/api/v0/transfers/downloads/flacfan/{transfer['id']}")).json()
        assert one["state"] == "Completed, Succeeded"
        listed = (await client.get("/api/v0/transfers/downloads")).json()
        assert listed[0]["username"] == "flacfan"
        assert listed[0]["directories"][0]["files"][0]["id"] == transfer["id"]


async def test_the_file_lands_where_slskd_puts_it(fake, client_for):
    async with client_for(fake) as client:
        search_id = await finished_search(client)
        responses = (await client.get(f"{SEARCH}/{search_id}/responses")).json()
        offer = next(r for r in responses if r["username"] == "mp3lover")["files"][0]
        file = {"filename": offer["filename"], "size": offer["size"]}
        with_destination = {
            "username": "mp3lover",
            "files": [file],
            "options": {"destination": "a/b"},
        }
        for body in (with_destination, with_destination, {"username": "mp3lover", "files": [file]}):
            assert (await client.post(BATCHES, json=body)).status_code == 201
            await wait_until(lambda: all(t.local_filename for t in fake.transfers.values()))
    name = "08 Daft Punk - Get Lucky.mp3"
    # With a batch destination: <downloads>/<destination>/<name>; a name that's taken gets
    # "_<ticks>" (slskd's default "exists: rename").
    first, second = sorted((fake.downloads_dir / "a" / "b").iterdir())
    assert first.name == name and second.name.startswith("08 Daft Punk - Get Lucky_")
    # Without one: slskd's default, the remote file's parent folder.
    parent = offer["filename"].split("\\")[-2]
    assert (fake.downloads_dir / parent / name).is_file()


async def test_one_search_request_at_a_time(make_fake, client_for):
    fake = make_fake(speed=1)  # posting a search takes 0.2 s here
    async with client_for(fake) as client:
        first, second = await asyncio.gather(
            client.post(SEARCH, json={"searchText": "daft punk get lucky"}),
            client.post(SEARCH, json={"searchText": "beyoncé halo"}),
        )
    assert sorted([first.status_code, second.status_code]) == [200, 429]


async def test_bad_requests(fake, client_for):
    async with client_for(fake) as client:
        assert (await client.post(SEARCH, json={"searchText": " "})).status_code == 400
        assert (await client.post(BATCHES, json={"username": "x", "files": []})).status_code == 400
        bad_id = {"id": "nope", "username": "x", "files": [{"filename": "a", "size": 1}]}
        assert (await client.post(BATCHES, json=bad_id)).status_code == 400
        assert (
            await client.get(f"{SEARCH}/5b0c4b6e-0d0a-4a64-9a1e-2f6d0f3c7a11")
        ).status_code == 404


async def test_scenario_words_and_flag(make_fake, client_for):
    fake = make_fake()
    assert fake.scenario_for("releasomat test stall") == "stall"
    assert fake.scenario_for(r"@@x\Releasomat Test\06 - wrongfile.flac") == "wrongfile"
    assert fake.scenario_for("daft punk get lucky") == "normal"
    assert make_fake(scenario="offline").scenario_for("daft punk get lucky") == "offline"
    with pytest.raises(ValueError):
        make_fake(scenario="unknown")
    async with client_for(fake) as client:
        search_id = await finished_search(client, "releasomat test noresults")
        assert (await client.get(f"{SEARCH}/{search_id}/responses")).json() == []
        exact = await finished_search(client, "releasomat test exactfails")
        assert (await client.get(f"{SEARCH}/{exact}/responses")).json() == []
        simple = await finished_search(client, "*eleasomat test exactfails")
        assert len((await client.get(f"{SEARCH}/{simple}/responses")).json()) > 3


def test_the_catalog_reads_a_library(tmp_path):
    from app.tagging import Tags, write_tags

    album = tmp_path / "Library" / "Queen" / "Jazz"
    album.mkdir(parents=True)
    song = album / "12 Don't Stop Me Now.flac"
    shutil.copyfile(Path(__file__).parent.parent / "fixtures" / "audio" / "tone.flac", song)
    write_tags(song, Tags(title="Don't Stop Me Now", artist="Queen", album="Jazz"))
    songs = load_catalog(tmp_path / "Library")
    assert songs[0].artist == "Queen" and songs[0].title == "Don't Stop Me Now"
    assert songs[0].album == "Jazz" and songs[0].length == 2
    assert songs[1:] == EXTRA_SONGS
    assert load_catalog(tmp_path / "missing") == EXTRA_SONGS


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
@pytest.mark.parametrize("ext", ["flac", "mp3", "aiff", "wav"])
def test_ffmpeg_makes_files_of_the_right_length(tmp_path, ext):
    from app.tagging import audio_info

    target = tmp_path / f"song.{ext}"
    ffmpeg_audio(target, ext, 7)
    info = audio_info(target)
    assert info.duration == pytest.approx(7, abs=0.2)
    if ext == "mp3":
        assert info.bitrate == 320
