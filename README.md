# OTTAM Production System

Autonomous production pipeline for the OTTAM YouTube channel.

## Phase 1 target

`topic -> research package -> script -> script QA -> Kokoro narration/timings -> narration-driven storyboard -> Magnific image generation -> visual QA -> FFmpeg render -> technical video QA -> final.mp4`

YouTube publishing is intentionally disabled until 2-3 finished videos pass quality review.

## Current implementation status

- xKiro free-model guard + streamed generation: implemented
- autonomous content stages: implemented (external source verification still to be added)
- Kokoro Modal async TTS + native timings/SRT: implemented
- storyboard + Magnific manifest: implemented
- scene-level visual QA: implemented
- deterministic FFmpeg renderer: implemented
- ffprobe technical QA: implemented
- Magnific transport: waiting for server-to-server authentication
- YouTube publisher: intentionally out of Phase 1

## Safety

The production workflow is manual-only during development. Unwired stages quarantine the episode; they never force incomplete content forward.
