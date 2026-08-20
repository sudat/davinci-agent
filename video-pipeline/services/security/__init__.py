"""Security verification tooling (Todo 66).

``probe_local_listeners`` inventories loopback-bound services on the local
machine so the pipeline can verify, against the real listener state, that
pipeline-owned services never expose a non-loopback TCP port. This module
performs no network egress: it reads ``lsof``/``ps`` output only.
"""
