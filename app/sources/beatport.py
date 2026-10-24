"""Beatport charts and Top 100 / Hype 100 lists (overall and per genre).

Beatport's pages are built with Next.js: the tracks are in the page as JSON
(``<script id="__NEXT_DATA__">``, under props → pageProps → dehydratedState → queries), with mix
name ("Extended Mix"), artists, ISRC, BPM, key and length. Titles keep the mix name: for a DJ,
"Think of You (Extended Mix)" and the radio edit are different tracks.

The page also holds a short-lived anonymous Beatport login ("anonSession"). Releasomat never uses
it, and it never goes into error reports. Shared user playlists (beatport.com/playlists/share/…)
load their tracks in the browser with that login, so Releasomat can't read them.
"""

import re

from app.httpclient import client
from app.sources.base import (
    PlaylistRef,
    PlaylistSnapshot,
    Source,
    SourceTrack,
    SourceType,
    UnsupportedLink,
    split_link,
)
from app.sources.web import (
    ms_to_seconds,
    page_title,
    request,
    require_tracks,
    script_ids,
    script_json,
    source_error,
)

SITE = "https://www.beatport.com"
LOCALES = {"en", "de", "fr", "es", "it", "ja", "pt", "nl"}
LISTS = {"top-100": "Top 100", "hype-100": "Hype 100"}
NUMBER = re.compile(r"^\d+$")
SLUG = re.compile(r"^[a-z0-9-]+$")
OTHER_KINDS = {
    "track": "a single track",
    "release": "a release (an album or EP)",
    "artist": "an artist page",
    "label": "a label page",
}
SCRIPT_ID = "__NEXT_DATA__"
COVER_SIZE = 1000
CHANGED = "Beatport changed its pages: Releasomat can't find the track list."
NOT_FOUND = "Beatport can't find this chart. It may have been removed."
SHARED = (
    "Shared Beatport playlists load their tracks only inside the browser, so Releasomat can't "
    "read them. Beatport charts and the Top 100 / Hype 100 lists work."
)


class Beatport(Source):
    type = SourceType.BEATPORT
    label = "Beatport"
    hosts = ("beatport.com",)
    examples = (
        "www.beatport.com/chart/…/…",
        "www.beatport.com/top-100",
        "www.beatport.com/genre/…/…/hype-100",
    )

    @classmethod
    def parse_url(cls, url: str) -> PlaylistRef:
        parts = split_link(url).parts
        if parts and parts[0] in LOCALES:  # beatport.com/de/top-100
            parts = parts[1:]
        if len(parts) == 1 and parts[0] in LISTS:
            return PlaylistRef(
                source=cls.type,
                id=parts[0],
                url=f"{SITE}/{parts[0]}",
                label=f"Beatport {LISTS[parts[0]]}",
            )
        if (
            len(parts) == 3
            and parts[0] == "chart"
            and SLUG.match(parts[1])
            and NUMBER.match(parts[2])
        ):
            return PlaylistRef(
                source=cls.type,
                id=f"chart/{parts[2]}",
                url=f"{SITE}/chart/{parts[1]}/{parts[2]}",
                label="Beatport chart",
            )
        if (
            len(parts) >= 3
            and parts[0] == "genre"
            and SLUG.match(parts[1])
            and NUMBER.match(parts[2])
        ):
            page = parts[3] if len(parts) > 3 else ""
            if page in LISTS:
                return PlaylistRef(
                    source=cls.type,
                    id=f"genre/{parts[2]}/{page}",
                    url=f"{SITE}/genre/{parts[1]}/{parts[2]}/{page}",
                    label=f"Beatport genre {LISTS[page]}",
                )
            if page == "charts":
                raise UnsupportedLink(
                    "This is a list of Beatport charts. Open one chart and paste its link."
                )
            raise UnsupportedLink(
                "This is a genre page on Beatport. Paste the link of its Top 100 or Hype 100."
            )
        if parts[:2] == ["playlists", "share"] or parts[:1] == ["library"]:
            raise UnsupportedLink(SHARED)
        if parts[:1] == ["charts"] or parts[:2] == ["dj", "charts"]:
            raise UnsupportedLink(
                "This is a list of Beatport charts. Open one chart and paste its link."
            )
        if parts and parts[0] in OTHER_KINDS:
            raise UnsupportedLink(f"This is {OTHER_KINDS[parts[0]]} on Beatport, not a chart.")
        raise UnsupportedLink(
            "Paste the link of a Beatport chart (beatport.com/chart/…) or of a Top 100 / Hype 100 "
            "list."
        )

    async def fetch(self, ref: PlaylistRef) -> PlaylistSnapshot:
        async with client() as http:
            response = await request(
                http, "GET", ref.url, service=self.label, errors={404: NOT_FOUND}
            )
        return parse_page(response.text, ref)


def parse_page(page: str, ref: PlaylistRef) -> PlaylistSnapshot:
    """The chart or list from the page's JSON (separate from fetch, so tests can feed pages)."""
    url = ref.url
    try:
        data = script_json(page, SCRIPT_ID)
    except ValueError as exc:
        raise source_error(CHANGED, url=url, error_type=type(exc).__name__) from exc
    if data is None:
        raise source_error(
            CHANGED,
            url=url,
            looked_for=f'<script id="{SCRIPT_ID}">',
            found_scripts=script_ids(page),
            page_title=page_title(page),
        )

    queries = _queries(data)
    track_list = next((q["data"] for q in queries if _is_track_list(q["data"])), None)
    if track_list is None:  # an empty chart still has its (empty) track query
        track_list = next((q["data"] for q in queries if _is_empty_track_list(q)), None)
    if track_list is None:
        # Only the names of what was found: the page's JSON also holds a Beatport login.
        raise source_error(
            CHANGED,
            url=url,
            looked_for="a query with a list of tracks (results with name, mix_name, artists)",
            found_queries=[q["key"] for q in queries][:20],
        )

    items = track_list["results"]
    tracks = [track for item in items if (track := _track(item))]
    require_tracks(tracks, "Beatport", url, results=len(items))
    warnings = []
    if len(tracks) < len(items):
        warnings.append(f"Skipped {len(items) - len(tracks)} entries without artist or title.")
    total = track_list.get("count")
    if track_list.get("next") and isinstance(total, int) and total > len(items):
        warnings.append(f"Beatport's page lists {len(items)} of {total} tracks.")

    chart = next((q["data"] for q in queries if _is_chart(q["data"])), None)
    if chart is not None:
        title = chart.get("name") or "Beatport chart"
        cover = _image(chart.get("image"))
        description = chart.get("description") or _chart_by(chart)
    else:
        title, cover, description = _list_title(ref, queries), None, None
    return PlaylistSnapshot(
        title=title, tracks=tracks, cover_url=cover, description=description, warnings=warnings
    )


def _queries(data: object) -> list[dict]:
    """The page's data queries as {"key": "chart-906471-tracks-…", "data": {…}}."""
    props = data.get("props") if isinstance(data, dict) else None
    page_props = (props or {}).get("pageProps") if isinstance(props, dict) else None
    state = (page_props or {}).get("dehydratedState") if isinstance(page_props, dict) else None
    queries = (state or {}).get("queries") if isinstance(state, dict) else None
    found = []
    for query in queries if isinstance(queries, list) else []:
        if not isinstance(query, dict):
            continue
        key = query.get("queryKey")
        key = " ".join(map(str, key)) if isinstance(key, list) else str(key)
        found.append({"key": key[:120], "data": (query.get("state") or {}).get("data")})
    return found


def _is_track_list(data: object) -> bool:
    results = data.get("results") if isinstance(data, dict) else None
    return (
        isinstance(results, list)
        and bool(results)
        and all(isinstance(r, dict) for r in results)
        and {"name", "mix_name", "artists"} <= set(results[0])
    )


def _is_empty_track_list(query: dict) -> bool:
    data = query["data"]
    return "tracks" in query["key"] and isinstance(data, dict) and data.get("results") == []


def _is_chart(data: object) -> bool:
    return isinstance(data, dict) and "track_count" in data and "name" in data


def _track(item: dict) -> SourceTrack | None:
    name = item.get("name")
    artists = [
        a["name"] for a in item.get("artists") or [] if isinstance(a, dict) and a.get("name")
    ]
    if not name or not artists:
        return None
    mix = item.get("mix_name")
    release = item.get("release") if isinstance(item.get("release"), dict) else {}
    key = item.get("key") if isinstance(item.get("key"), dict) else {}
    bpm = item.get("bpm")
    return SourceTrack(
        artist=", ".join(artists),
        title=f"{name} ({mix})" if mix else name,
        album=release.get("name") or None,
        duration=ms_to_seconds(item.get("length_ms")),
        isrc=item.get("isrc") or None,
        cover_url=_image(release.get("image")),
        source_id=str(item["id"]) if item.get("id") else None,
        bpm=float(bpm) if isinstance(bpm, int | float) and bpm > 0 else None,
        key=key.get("name") or None,
    )


def _image(image: object) -> str | None:
    """A Beatport picture at COVER_SIZE ("…/image_size/{w}x{h}/…"), else as it is."""
    if not isinstance(image, dict):
        return None
    template = image.get("dynamic_uri")
    if isinstance(template, str) and "{w}" in template:
        return template.replace("{w}", str(COVER_SIZE)).replace("{h}", str(COVER_SIZE))
    return image.get("uri") or None


def _chart_by(chart: dict) -> str | None:
    person = chart.get("person") if isinstance(chart.get("person"), dict) else {}
    owner = person.get("owner_name") or (chart.get("artist") or {}).get("name")
    return f"A Beatport chart by {owner}." if owner else None


def _list_title(ref: PlaylistRef, queries: list[dict]) -> str:
    """'Beatport Top 100', 'Beatport House Hype 100'."""
    kind = LISTS.get(ref.id.rsplit("/", 1)[-1], "Top 100")
    if ref.id.startswith("genre/"):
        genre_id = ref.id.split("/")[1]
        genre = next(
            (
                q["data"].get("name")
                for q in queries
                if isinstance(q["data"], dict)
                and str(q["data"].get("id")) == genre_id
                and "sub_genres" in q["data"]
            ),
            None,
        )
        if genre:
            return f"Beatport {genre} {kind}"
    return f"Beatport {kind}"
