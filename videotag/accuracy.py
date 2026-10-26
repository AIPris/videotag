"""How far the people matching can be trusted, worked out from the reference faces of every run.

Everything here is arithmetic on the reference faces a run has already built, so it costs nothing and is as fresh as the
latest run. For each named person: how many reference faces there are and of what kind, the threshold used, whether
confident tags are allowed, how similar the person's own faces are to each other (leave-one-out) against how similar
other named people's faces are to them, and which pairs of people the matcher would mix up.
"""
import json
import os
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import numpy as np

from .calibration import leave_one_out, stranger_scores
from .matcher import DEFAULT_MATCH, PersonModel, face_similarity
from .references import ReferenceSet

ACCURACY_NAME = "accuracy.json"  # beside the reports, but not one (reports are run-*.json)


def _spread(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "min": None, "mean": None, "max": None}
    return {"n": len(values), "min": min(values), "mean": float(np.mean(values)), "max": max(values)}


def _mixed_up(sets: list[ReferenceSet], models: list[PersonModel]) -> list[dict]:
    """For each reference face, whom the matcher would give it to if the face itself were left out of its own person's
    references. A face that goes to somebody else, and clears their threshold, is one that could be tagged wrongly."""
    tally: Counter = Counter()
    for refs in (refs for refs in sets if refs.size > 1):
        for index, face in enumerate(refs.embeddings):
            own = face_similarity(face, np.delete(refs.embeddings, index, axis=0))
            scores = [own if model.person_id == refs.person_id else face_similarity(face, model.references) for model in models]
            taker = models[int(np.argmax(scores))]
            if taker.person_id != refs.person_id and max(scores) >= taker.threshold:
                tally[(refs.name, taker.name)] += 1
    size = {refs.name: refs.size for refs in sets}
    return [{"person": person, "taken_for": taken_for, "faces": faces, "of": size[person]}
            for (person, taken_for), faces in sorted(tally.items(), key=lambda item: (-item[1], item[0]))]


def _lookalikes(sets: list[ReferenceSet]) -> list[dict]:
    """Pairs where one person's faces resemble another's at least as much as the least similarity that ever counts as a
    match. Those are the pairs the thresholds had to be raised for. `faces` is how many of the person's faces do."""
    found = []
    for refs in sets:
        for other in sets:
            if other.person_id == refs.person_id:
                continue
            scores = [face_similarity(face, other.embeddings) for face in refs.embeddings]
            close = [score for score in scores if score >= DEFAULT_MATCH]
            if close:
                found.append({"person": refs.name, "resembles": other.name, "similarity": max(close), "faces": len(close),
                              "of": refs.size})
    return sorted(found, key=lambda pair: (-pair["similarity"], pair["person"]))


def report(sets: list[ReferenceSet], models: list[PersonModel]) -> dict:
    by_id = {model.person_id: model for model in models}
    people = []
    for refs in sets:
        model = by_id[refs.person_id]
        same = leave_one_out(refs.embeddings) if refs.size > 1 else []
        others = stranger_scores(refs, [other for other in sets if other.person_id != refs.person_id])
        same_spread, others_spread = _spread(same), _spread(others)
        separation = same_spread["mean"] - others_spread["max"] if same and others else None
        people.append({"person_id": refs.person_id, "name": refs.name, "references": refs.size,
                       "kinds": dict(Counter(source.kind for source in refs.sources)), "threshold": model.threshold,
                       "confident_allowed": model.can_auto, "same_person": same_spread, "others": others_spread,
                       "separation": separation, "recognised": {"n": sum(score >= model.threshold for score in same),
                                                                "of": len(same)}})
    return {"people": people, "lookalikes": _lookalikes(sets), "mixed_up": _mixed_up(sets, models)}


def save(path: Path, result: dict, clock: Callable[[], datetime] = datetime.now) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + ".part")
    scratch.write_text(json.dumps({"time": clock().strftime("%Y-%m-%dT%H:%M:%S"), **result}, indent=2), encoding="utf-8")
    os.replace(scratch, path)  # a dashboard reading meanwhile sees the old file or the new one
