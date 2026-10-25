"""Describing a video with a local vision-language model, in English and in German.

The model is only imported when the first video actually needs describing, so runs that find everything in
the cache never load it. Answers are parsed strictly: a place is kept only when the model names one and cites
the landmark or readable text that shows it (the model's own confidence rating proved unreliable).
"""
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

MODEL_ID = "Qwen/Qwen3-VL-4B-Instruct"
PROMPT_VERSION = "v1"  # bumping it makes every video get described again
FRAMES = 6
MAX_SIDE = 640
MAX_NEW_TOKENS = 700
MAX_TOPICS, MAX_OBJECTS = 5, 8

PROMPT = """These are {n} frames from one short video, in order. Describe what is visible for a photo-library search index, in English and in German.
Answer with JSON only, no other text, in exactly this shape:
{{"summary": {{"en": "<one plain sentence>", "de": "<the same sentence in German>"}}, "topics": {{"en": ["<2-5 broad themes or activities, lower case>"], "de": ["<the same in German>"]}}, "objects": {{"en": ["<up to 8 notable objects or animals, lower case, singular>"], "de": ["<the same in German>"]}}, "place": null or {{"name": "<city, landmark or country>", "evidence": "<the landmark or the readable text that shows it>"}}}}
Rules: describe only what is visible. Do not name people, and do not guess brands or product names. Set "place" to null unless a famous landmark or readable signage clearly shows where it is."""

_NO_EVIDENCE = re.compile(r"^(no|none|not|n/a)\b", re.IGNORECASE)
_NOT_A_PLACE = {"null", "none", "unknown", "n/a"}


@dataclass(frozen=True)
class Description:
    summary_en: str
    summary_de: str
    topics_en: tuple[str, ...] = ()
    topics_de: tuple[str, ...] = ()
    objects_en: tuple[str, ...] = ()
    objects_de: tuple[str, ...] = ()
    place: str | None = None
    place_evidence: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Description":
        return cls(**{key: tuple(value) if isinstance(value, list) else value for key, value in data.items()})


def evenly(frames: list[Path], count: int) -> list[Path]:
    """`count` frames spread over the whole video (all of them when there are fewer)."""
    if len(frames) <= count:
        return frames
    return [frames[round(i * (len(frames) - 1) / (count - 1))] for i in range(count)]


def _text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _texts(value, limit: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    unique = dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip())
    return tuple(list(unique)[:limit])


def _language(data: dict, key: str, language: str):
    section = data.get(key)
    return section.get(language) if isinstance(section, dict) else None


def parse_description(answer: str) -> Description | None:
    """The model's JSON answer as a Description, or None when it is unusable."""
    try:
        data = json.loads(answer[answer.index("{"): answer.rindex("}") + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    summary_en, summary_de = _text(_language(data, "summary", "en")), _text(_language(data, "summary", "de"))
    if summary_en is None or summary_de is None:
        return None
    place, evidence = None, None
    raw_place = data.get("place")
    if isinstance(raw_place, dict):
        name, cited = _text(raw_place.get("name")), _text(raw_place.get("evidence"))
        if name and name.lower() not in _NOT_A_PLACE and cited and not _NO_EVIDENCE.match(cited):
            place, evidence = name, cited
    return Description(
        summary_en, summary_de,
        _texts(_language(data, "topics", "en"), MAX_TOPICS), _texts(_language(data, "topics", "de"), MAX_TOPICS),
        _texts(_language(data, "objects", "en"), MAX_OBJECTS), _texts(_language(data, "objects", "de"), MAX_OBJECTS),
        place, evidence,
    )


class Describer:
    def __init__(self) -> None:
        self._model = None
        self._processor = None

    def _load(self) -> None:
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor

        self._processor = AutoProcessor.from_pretrained(MODEL_ID)
        self._model = AutoModelForImageTextToText.from_pretrained(MODEL_ID, dtype=torch.bfloat16, device_map="cuda")

    def describe(self, frames: list[Path]) -> Description | None:
        """None when the model's answer was unusable; model or GPU failures raise."""
        import torch
        from PIL import Image

        if self._model is None:
            self._load()
        images = []
        for path in frames:
            image = Image.open(path).convert("RGB")
            image.thumbnail((MAX_SIDE, MAX_SIDE))
            images.append(image)
        content = [{"type": "image", "image": image} for image in images]
        content.append({"type": "text", "text": PROMPT.format(n=len(images))})
        inputs = self._processor.apply_chat_template([{"role": "user", "content": content}], tokenize=True,
                                                     add_generation_prompt=True, return_dict=True,
                                                     return_tensors="pt").to(self._model.device)
        with torch.no_grad():
            generated = self._model.generate(**inputs, max_new_tokens=MAX_NEW_TOKENS, do_sample=False)
        answer = self._processor.batch_decode(generated[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)[0]
        return parse_description(answer)
