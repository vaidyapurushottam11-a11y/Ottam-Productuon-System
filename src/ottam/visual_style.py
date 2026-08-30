from __future__ import annotations

from dataclasses import dataclass


BASE_PREFIX = (
    "Hand-drawn 2D stickman animation, pure minimalist black line stick figure "
    "with a perfectly round plain white circle head, dot eyes and flat line mouth, "
    "stick body with single-line arms and legs, NO clothing, NO color fill on the figure, "
    "NO shading on the figure, bold clean black outlines only, drawn like a rough kid's "
    "notebook doodle, set against a crude, loosely hand-drawn full-color background with "
    "flat color washes and visibly wobbly, imperfect linework, "
)

BASE_SUFFIX_NO_TEXT = (
    ", no photorealism, no 3D rendering, no digital painting, no airbrushed gradients, "
    "no soft cinematic lighting, no glossy finish, no fine detail, no realistic textures, "
    "background kept sparse and childlike, absolutely NO text, NO words, NO letters, "
    "NO writing, NO posters, NO signage, NO labels anywhere in the image unless a single "
    "hand-lettered word is explicitly specified in this prompt, stick figure itself remains "
    "pure black line only with no clothing and no color fill on its body, 16:9 aspect ratio, "
    "Ottam rough hand-drawn stickman explainer style."
)

BASE_SUFFIX_WITH_TEXT = (
    ", no photorealism, no 3D rendering, no digital painting, no airbrushed gradients, "
    "no soft cinematic lighting, no glossy finish, no fine detail, no realistic textures, "
    "background kept sparse and childlike, stick figure itself remains pure black line only "
    "with no clothing and no color fill on its body, 16:9 aspect ratio, Ottam rough "
    "hand-drawn stickman explainer style."
)


@dataclass(frozen=True)
class PromptSpec:
    scene_description: str
    allowed_text: str | None = None


def build_magnific_prompt(spec: PromptSpec) -> str:
    """Wrap a scene-specific visual idea in the locked OTTAM prompt shell.

    The sample Episode 05 prompt table used a highly repetitive style contract across
    essentially every frame. Keeping that contract deterministic prevents model/style
    drift and lets the LLM focus only on the visual concept for the current narration.
    """
    scene = spec.scene_description.strip().strip(" ,")
    if not scene:
        raise ValueError("scene_description must not be empty")

    if spec.allowed_text:
        word = spec.allowed_text.strip()
        text_rule = (
            f', the ONLY text in this image is the single hand-lettered word "{word}", '
            "nothing else is written or labelled anywhere"
        )
        return BASE_PREFIX + scene + text_rule + BASE_SUFFIX_WITH_TEXT

    return BASE_PREFIX + scene + BASE_SUFFIX_NO_TEXT
