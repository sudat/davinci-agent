"""Single-writer Clean Builder over disposable Resolve staging timelines.

The builder promotes only live-verified bridge operations, never mutates
human timelines, never resumes a partial build, and writes no Job State.
"""
