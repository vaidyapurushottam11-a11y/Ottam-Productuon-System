from __future__ import annotations

from dataclasses import dataclass


CHARACTER_CONTINUITY = (
    "EPISODE-WIDE CHARACTER CONTINUITY IS MANDATORY: treat the main stick figure as the same recurring protagonist in every scene. "
    "Keep the exact same perfectly round white head, dot-eye style, mouth style, head-to-body scale, limb proportions, line thickness, and minimalist anatomy throughout the entire episode. "
    "Recurring secondary stick figures must likewise keep their established body proportions and facial construction from scene to scene. "
    "Camera distance, pose, gesture, emotion, and facial expression may change to fit the narration, but character DESIGN and identity must not change. "
    "Do not invent hair, clothing, hats, accessories, facial hair, skin fill, body fill, different head shapes, realistic hands, different drawing styles, or extra character details in later scenes. "
)

BASE_PREFIX = (
    "Hand-drawn 2D stickman animation, pure minimalist black line stick figure "
    "with a perfectly round plain white circle head, dot eyes and flat line mouth, "
    "stick body with single-line arms and legs, NO clothing, NO color fill on the figure, "
    "NO shading on the figure, bold clean black outlines only, drawn like a rough kid's "
    "notebook doodle. "
    + CHARACTER_CONTINUITY
    + "Set against a crude, loosely hand-drawn full-color background with "
    "flat color washes and visibly wobbly, imperfect linework, "
)

BASE_SUFFIX_NO_TEXT = (
    ", no photorealism, no 3D rendering, no digital painting, no airbrushed gradients, "
    "no soft cinematic lighting, no glossy finish, no fine detail, no realistic textures, "
    "background kept sparse and childlike, absolutely NO text, NO words, NO letters, "
    "NO writing, NO posters, NO signage, NO labels anywhere in the image unless a single "
    "hand-lettered word is explicitly specified in this prompt, stick figure itself remains "
    "pure black line only with no clothing and no color fill on its body, preserve the exact recurring character design from the rest of this episode, "
    "16:9 aspect ratio, Ottam rough hand-drawn stickman explainer style."
)

BASE_SUFFIX_WITH_TEXT = (
    ", no photorealism, no 3D rendering, no digital painting, no airbrushed gradients, "
    "no soft cinematic lighting, no glossy finish, no fine detail, no realistic textures, "
    "background kept sparse and childlike, stick figure itself remains pure black line only "
    "with no clothing and no color fill on its body, preserve the exact recurring character design from the rest of this episode, "
    "16:9 aspect ratio, Ottam rough hand-drawn stickman explainer style."
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
    Character continuity is also repeated in every independent Magnific request because
    scenes are generated separately and must not reinterpret recurring figures.
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
