"""What this tool remembers between runs: the tags it wrote and what the user did about them."""
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import STATE_DIR

STATE_FILE = STATE_DIR / "state.json"


@dataclass(frozen=True)
class WrittenTag:
    item_id: int
    person_id: int
    tag_id: int
    tag_name: str
    level: str      # "auto" or "review"
    added_at: str


class StateError(RuntimeError):
    pass


@dataclass
class State:
    path: Path = STATE_FILE
    written: list[WrittenTag] = field(default_factory=list)       # tags this tool added and that are still there
    rejected: set[tuple[int, int]] = field(default_factory=set)   # (video, person): the user removed our tag
    confirmed: set[tuple[int, int]] = field(default_factory=set)  # (video, person): the user tagged it themselves
    albums: dict[int, int] = field(default_factory=dict)          # person id -> id of the album this tool created
    faces: dict[tuple[int, int], int] = field(default_factory=dict)  # (video, person) -> id of the face record this tool created
    descriptions: dict[int, str] = field(default_factory=dict)    # video -> the description block this tool wrote
    description_rejected: set[int] = field(default_factory=set)   # videos whose block the user deleted

    @classmethod
    def load(cls, path: Path = STATE_FILE) -> "State":
        """A missing file is an empty state. An unreadable one stops the run: guessing could re-add rejected tags."""
        if not path.exists():
            return cls(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))  # editors on Windows may add a BOM
            return cls(path, [WrittenTag(**row) for row in raw["written"]],
                       {tuple(pair) for pair in raw["rejected"]}, {tuple(pair) for pair in raw["confirmed"]},
                       {int(person): album for person, album in raw.get("albums", {}).items()},
                       {(row["item_id"], row["person_id"]): row["face_id"] for row in raw.get("faces", [])},
                       {int(video): block for video, block in raw.get("descriptions", {}).items()},
                       set(raw.get("description_rejected", [])))
        except (ValueError, KeyError, TypeError) as exc:
            raise StateError(f"state file {path} is unreadable ({type(exc).__name__}); fix or delete it") from None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "written": [asdict(tag) for tag in self.written],
            "rejected": sorted(self.rejected),
            "confirmed": sorted(self.confirmed),
            "albums": {str(person): album for person, album in self.albums.items()},
            "faces": [{"item_id": item, "person_id": person, "face_id": face}
                      for (item, person), face in sorted(self.faces.items())],
            "descriptions": {str(video): block for video, block in sorted(self.descriptions.items())},
            "description_rejected": sorted(self.description_rejected),
        }, indent=2), encoding="utf-8")
