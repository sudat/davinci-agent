"""Presentation layer (Phase 3): profiles, asset registry, manifests.

Todo 55 implements the minimal Phase-3 surface from the frozen Todo-56
inputs: layered presentation profiles with narrowing-only resolution, a
rights-gated asset registry, immutable Job-start snapshots, and
deterministic Presentation Manifest compilation. There is deliberately no
profile self-update path and no network fetch path anywhere in this package.
"""
