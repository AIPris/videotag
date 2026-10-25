"""Pure logic: decide which face in a group photo belongs to which listed person."""
import numpy as np

from .matcher import face_similarity

MIN_SCORE = 0.45  # a face must resemble the person's seed faces at least this much
MARGIN = 0.10     # and beat the next best candidate (other face for that person, other person for that face)


def assign_faces(faces: list[np.ndarray], seeds: dict[int, np.ndarray],
                 min_score: float = MIN_SCORE, margin: float = MARGIN) -> dict[int, tuple[int, float]]:
    """Map person id to (face index, similarity) for the people whose face can be told with confidence.

    `seeds` holds known faces only for the people Photos lists on this photo. Assignment is one to one;
    ambiguous faces are left out rather than guessed.
    """
    person_ids = list(seeds)
    if not faces or not person_ids:
        return {}
    scores = np.array([[face_similarity(face, seeds[pid]) for pid in person_ids] for face in faces])
    ranked = sorted(((scores[f, p], f, p) for f in range(len(faces)) for p in range(len(person_ids))), reverse=True)

    assigned: dict[int, tuple[int, float]] = {}
    used_faces: set[int] = set()
    for score, f, p in ranked:
        if score < min_score:
            break
        if person_ids[p] in assigned or f in used_faces:
            continue
        rivals = np.concatenate([np.delete(scores[:, p], f), np.delete(scores[f, :], p)])
        if rivals.size and score - rivals.max() < margin:
            continue
        assigned[person_ids[p]] = (f, float(score))
        used_faces.add(f)
    return assigned
