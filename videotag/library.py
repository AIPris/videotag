"""The Photos library split into photos and videos, plus the local cache of downloaded photos."""
from dataclasses import dataclass
from pathlib import Path

from .config import CACHE_DIR
from .synology import PhotosClient


@dataclass(frozen=True)
class Library:
    photos: list[dict]
    videos: list[dict]


def load_library(client: PhotosClient) -> Library:
    items = client.items(additional=("person", "tag", "description"))
    return Library(
        photos=[item for item in items if item["type"] == "photo"],
        videos=[item for item in items if item["type"] == "video"],
    )


def listed_people(item: dict) -> list[dict]:
    """Everyone Photos lists on the item, named or not."""
    return item["additional"]["person"]


def fetch(client: PhotosClient, item: dict) -> Path:
    """Download a photo's original once; later calls reuse the cached file."""
    dest = CACHE_DIR / "photos" / f"{item['id']}{Path(item['filename']).suffix.lower()}"
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    return client.download(item["id"], dest)


def _cover_key(person: dict) -> str | None:
    return person.get("additional", {}).get("thumbnail", {}).get("cache_key")


def fetch_cover(client: PhotosClient, person: dict) -> Path | None:
    """The person's cover face crop. Cached by cache key, so a new cover in Photos is fetched again."""
    key = _cover_key(person)
    if key is None:
        return None
    dest = CACHE_DIR / "covers" / f"{person['id']}_{key}.jpg"
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    return client.person_cover(person["id"], key, dest)


def cover_source_photo(person: dict) -> int | None:
    """The photo the cover face was cut from (the cache key starts with its item id)."""
    key = _cover_key(person)
    return int(key.split("_")[0]) if key else None
