"""Embeddings, fingerprints, vector index and story clustering.

Import the submodules directly (e.g. `radar.clustering.service`). The package deliberately does
not re-export them: `radar.normalization.normalize` imports `radar.clustering.fingerprint`, and
an eager `service` import here would close the cycle normalize -> clustering -> service -> normalize.
"""
