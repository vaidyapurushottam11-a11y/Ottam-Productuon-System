from __future__ import annotations

from pathlib import Path

from .magnific_api import MagnificApiClient
from .visual_style import PromptSpec, build_magnific_prompt


SMOKE_SCENE = (
    "A single black stick figure sits on the edge of a simple bed at night, "
    "leaning forward with elbows on knees and looking worried. A small bedside "
    "table and window establish the bedroom. Keep the composition sparse and readable."
)


def main() -> None:
    output_dir = Path("runtime/smoke")
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt = build_magnific_prompt(PromptSpec(scene_description=SMOKE_SCENE))
    image, metadata = MagnificApiClient().generate_image(prompt)
    (output_dir / "magnific-smoke.png").write_bytes(image)
    (output_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    (output_dir / "metadata.txt").write_text(str(metadata), encoding="utf-8")


if __name__ == "__main__":
    main()
