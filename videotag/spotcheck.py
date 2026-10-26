"""A random sample of the matches of each person, to look at in Photos and judge by eye.

The sample is drawn from a report (dry or applied). Confident matches and suggestions are sampled separately, because a
mistake is far more likely among the suggestions. What the user already settled (confirmed or removed in Photos) is not
asked about again, a copy of a file counts once, and a video that failed is not a match.
"""
import random

from .state import State


def sample(rows: list[dict], state: State, count: int, rng: random.Random) -> list[dict]:
    """For each person with matches, in name order: how many confident matches and suggestions there are, and up to
    `count` of each, as `{"video_id", "filename", "folder_id"}`."""
    if count < 1:
        raise ValueError("a spot-check needs at least one video per person")
    found: dict[int, dict] = {}
    for row in rows:
        if "error" in row or row.get("twin_of") is not None:
            continue
        for match in row.get("matches", []):
            key = (row["video_id"], match["person_id"])
            if match["decision"] not in ("auto", "review") or key in state.confirmed or key in state.rejected:
                continue
            person = found.setdefault(match["person_id"], {"name": match["name"], "auto": [], "review": []})
            person[match["decision"]].append({"video_id": row["video_id"], "filename": row["filename"],
                                              "folder_id": row.get("folder_id")})

    def draw(matches: list[dict]) -> dict:
        return {"total": len(matches), "sample": rng.sample(matches, min(count, len(matches)))}

    return [{"id": person_id, "name": person["name"], "confident": draw(person["auto"]), "suggested": draw(person["review"])}
            for person_id, person in sorted(found.items(), key=lambda item: item[1]["name"].lower())]
