"""Semantic-contract publisher (Synapse step 21.5).

After a successful indexing sync, a region derives a **semantic contract** —
a bounded JSON projection of its own content — and writes it to
``<state>/contract.latest.json`` (durable tmp + fsync + rename). The router
(pheasant-flock) scores these contracts at Tier 1 to decide which
regions to query; it never reads region internals at routing time.

Wire format
-----------
The contract is built **by hand** to match the vendored JSON Schema
``contracts/semantic_contract.v1.schema.json`` exactly, and the integrity
content hash / file serialization match the sibling's canonical
``SemanticContract.seal()`` / ``to_json_text()`` byte-for-byte:

- All embedding vectors travel as base64 of their **float16** bytes
  (:func:`_encode_fp16_b64`), length == ``embedding_space.dim``.
- The MinHash signature travels as base64 of a 128-element **uint64** array
  (:func:`_encode_u64_b64`); permutation ``i`` hashes term ``t`` as the low
  64 bits of ``blake2b(f"{i}:{t}", digest_size=8)`` (big-endian), min across
  terms. This matches the sibling's ``minhash_signature`` exactly so the
  white-matter overlap pre-filter can compare MinHashes across repos.
- ``integrity.content_hash`` = ``"sha256:" + sha256(canonical_json)`` over the
  whole body with ``integrity`` excluded, ``sort_keys=True``,
  ``separators=(",", ":")``, ``ensure_ascii=False``.
- The file is ``json.dumps(..., indent=2, sort_keys=True, ensure_ascii=False)``
  plus a trailing newline.

Signature derivation
---------------------
When embed-on-sync (step 21.4) is enabled the publisher streams the region's
chunk vectors from the configured :class:`VectorStore`:

- ``centroid`` = mean vector, ``covariance_diag`` = population variance per dim.
- ``cluster_centroids`` (≤32) + ``cluster_weights`` from a deterministic,
  seeded Lloyd's k-means over a strided sample (no sklearn dependency; pure
  numpy so it works without the ``[vector]`` extra). Clusters are canonically
  ordered by ``(-weight, fp16_payload)`` for byte-stable output.

Watermark
---------
``source_checkpoints_digest`` is sha256 over each source's ordered
``(source_id, relative_path, sha256)`` content fingerprint from the artifacts
table — the region's true content identity. It deliberately excludes the
connector checkpoints' transient per-sync counters (indexed/skipped), so two
syncs with unchanged content yield the **identical** digest (the freshness
block only changes iff region content changed).

No-vector fallback
------------------
If embeddings are disabled there are no chunk vectors. Rather than make the
publisher a no-op (which would leave the router unable to route on the
region's vocabulary/freshness at all), it emits a contract with a **degenerate
zero signature** (zero centroid, zero covariance, no clusters,
``sample_count=0``) and ``embedding_space`` taken from the configured embed
model/dim. Capabilities truthfully report ``returns_vectors=false`` and omit
``"vector"`` from ``search_modes``. This still validates against the schema and
carries the vocabulary + watermark, which is the honest, useful option.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from pheasant.config.schema import EmbeddingsSettings, PheasantConfig
    from pheasant.persistence.state_store import StateStore
    from pheasant.search.vector_store import VectorStore

logger = logging.getLogger(__name__)

CONTRACT_FILENAME = "contract.latest.json"
SCHEMA_VERSION = 1
MAX_CLUSTERS = 32
MAX_VOCAB_TERMS = 256
MAX_CONTRACT_BYTES = 256 * 1024
MINHASH_PERMS = 128
KMEANS_SAMPLE_CAP = 16_384
KMEANS_MAX_ITERS = 25

# Native output width of well-known OpenAI-spec models, used only as a
# fallback when `search.embeddings.dimensions` is unset (the default —
# meaning "use the model's own size") AND nothing has been embedded onto
# disk yet for `_resolved_dimension` to read the true width from. Once a
# sync has embedded at least one chunk, the on-disk width wins and this
# table stops mattering for that region.
_KNOWN_MODEL_DIMENSIONS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}
# StubEmbedder's own built-in default (see search/vector_store.py) — the
# last-resort fallback for a model this table doesn't recognize.
DEFAULT_STUB_DIMENSION = 64


def contract_path(state_path: str | Path) -> Path:
    return Path(state_path) / CONTRACT_FILENAME


def load_contract_text(state_path: str | Path) -> str | None:
    """Return the on-disk contract JSON text, or ``None`` if not published."""

    path = contract_path(state_path)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Wire-format codecs — must match pheasant_flock/synapse/contract.py
# ---------------------------------------------------------------------------


def _encode_fp16_b64(values: Any) -> str:
    arr = np.asarray(values, dtype=np.float16)
    if arr.ndim != 1:
        raise ValueError(f"expected a 1-D vector, got shape {arr.shape}")
    return base64.b64encode(arr.tobytes()).decode("ascii")


def _encode_u64_b64(values: Any) -> str:
    arr = np.asarray(values, dtype=np.uint64)
    if arr.ndim != 1:
        raise ValueError(f"expected a 1-D signature, got shape {arr.shape}")
    return base64.b64encode(arr.tobytes()).decode("ascii")


def _minhash_signature(terms: list[str], num_perm: int = MINHASH_PERMS) -> np.ndarray:
    """Deterministic MinHash, byte-identical to the sibling's scheme."""

    max_u64 = np.iinfo(np.uint64).max
    if not terms:
        return np.full(num_perm, max_u64, dtype=np.uint64)
    sig = np.full(num_perm, max_u64, dtype=np.uint64)
    for term in terms:
        for i in range(num_perm):
            digest = hashlib.blake2b(f"{i}:{term}".encode(), digest_size=8).digest()
            h = np.uint64(int.from_bytes(digest, "big"))
            if h < sig[i]:
                sig[i] = h
    return sig


def _content_hash(body: dict[str, Any]) -> str:
    """sha256 over canonical JSON of the body (``integrity`` excluded)."""

    payload = {k: v for k, v in body.items() if k != "integrity"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _to_json_text(contract: dict[str, Any]) -> str:
    return json.dumps(contract, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Signature math (pure numpy, deterministic)
# ---------------------------------------------------------------------------


def _seeded_kmeans(
    matrix: np.ndarray,
    k: int,
    *,
    seed: int = 1337,
    max_iters: int = KMEANS_MAX_ITERS,
) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic Lloyd's k-means; returns (centroids, weights).

    Weights are the fraction of points assigned to each centroid. Empty
    clusters are dropped. Initialization is the first ``k`` distinct rows of a
    seed-shuffled view, so the result is reproducible across runs/platforms.
    """

    n = matrix.shape[0]
    k = max(1, min(k, n))
    rng = np.random.default_rng(seed)
    order = rng.permutation(n)
    centroids = matrix[order[:k]].astype(np.float64).copy()
    assignments = np.zeros(n, dtype=np.int64)
    for _ in range(max_iters):
        # cosine-free euclidean assignment is fine here; vectors are unit-norm
        dists = np.linalg.norm(matrix[:, None, :] - centroids[None, :, :], axis=2)
        new_assignments = np.argmin(dists, axis=1)
        if np.array_equal(new_assignments, assignments):
            assignments = new_assignments
            break
        assignments = new_assignments
        for c in range(centroids.shape[0]):
            members = matrix[assignments == c]
            if len(members):
                centroids[c] = members.mean(axis=0)
    counts = np.bincount(assignments, minlength=centroids.shape[0]).astype(np.float64)
    keep = counts > 0
    centroids = centroids[keep]
    counts = counts[keep]
    weights = counts / counts.sum()
    return centroids, weights


def _signature_from_vectors(vectors: np.ndarray, *, max_clusters: int, seed: int) -> dict[str, Any]:
    """Build the signature block from a stack of chunk vectors."""

    sample_count = int(vectors.shape[0])
    centroid = vectors.mean(axis=0)
    covariance_diag = vectors.var(axis=0)  # population variance
    m = min(max_clusters, int(np.floor(np.sqrt(sample_count))) or 1, sample_count)
    # Strided sample cap so k-means cost is bounded for large regions.
    sample = vectors
    if sample_count > KMEANS_SAMPLE_CAP:
        stride = sample_count // KMEANS_SAMPLE_CAP
        sample = vectors[::stride][:KMEANS_SAMPLE_CAP]
    centroids, weights = _seeded_kmeans(sample, m, seed=seed)
    cluster_payloads = [_encode_fp16_b64(c) for c in centroids]
    # Canonical ordering: heaviest first, fp16 payload as the tie-break.
    ordered = sorted(
        zip(cluster_payloads, (float(w) for w in weights), strict=True),
        key=lambda pair: (-pair[1], pair[0]),
    )
    return {
        "centroid": _encode_fp16_b64(centroid),
        "covariance_diag": _encode_fp16_b64(covariance_diag),
        "cluster_centroids": [payload for payload, _ in ordered],
        "cluster_weights": [weight for _, weight in ordered],
        "sample_count": sample_count,
    }


def _degenerate_signature(dim: int) -> dict[str, Any]:
    zeros = np.zeros(dim, dtype=np.float64)
    return {
        "centroid": _encode_fp16_b64(zeros),
        "covariance_diag": _encode_fp16_b64(zeros),
        "cluster_centroids": [],
        "cluster_weights": [],
        "sample_count": 0,
    }


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------


class ContractPublisher:
    """Derive + write the semantic contract from the region's own content."""

    def __init__(
        self,
        config: PheasantConfig,
        state: StateStore,
        *,
        vector_store: VectorStore | None = None,
        seed: int = 1337,
    ):
        self.config = config
        self.state = state
        self.vector_store = vector_store
        self.seed = seed

    # -- public API --------------------------------------------------

    def build(self, *, generated_at: str | None = None) -> dict[str, Any]:
        """Build the sealed contract dict (does not write it)."""

        from pheasant.ingestion.pipeline import utc_now

        generated_at = generated_at or utc_now()
        embedding_space = self._embedding_space()
        body: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "kind": "semantic_contract",
            "kb_id": self.config.knowledge_base_id,
            "fleet_id": self._fleet_id(),
            "endpoint": self._endpoint(),
            "generated_at": generated_at,
            "watermark": self._watermark(generated_at),
            "capabilities": self._capabilities(),
            "embedding_space": embedding_space,
            "signature": self._signature(embedding_space["dim"]),
            "vocabulary": self._vocabulary(),
            "axiom_projections": {},
        }
        body["integrity"] = {
            "content_hash": _content_hash(body),
            "signature": self._signature_b64(body),
        }
        return body

    def publish(self, *, generated_at: str | None = None) -> dict[str, Any]:
        """Build + durably write ``<state>/contract.latest.json``."""

        contract = self.build(generated_at=generated_at)
        text = _to_json_text(contract)
        size = len(text.encode("utf-8"))
        if size > MAX_CONTRACT_BYTES:
            raise ValueError(
                f"semantic contract for kb_id={contract['kb_id']!r} serializes to "
                f"{size} bytes (cap {MAX_CONTRACT_BYTES}); shrink clusters/vocab"
            )
        path = contract_path(self.config.pheasant.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return contract

    # -- contract blocks ---------------------------------------------

    def _signature_b64(self, body: dict[str, Any]) -> str | None:
        """Ed25519-sign the canonical body when ``synapse.signing_key_ref`` is set.

        Returns ``None`` (unsigned) when no key ref is configured — the
        long-standing standalone path. The signature covers the same bytes as
        ``_content_hash`` so the router's ``verify_signature`` accepts it (the
        cross-repo crypto parity guaranteed by the parity test).
        """
        ref = getattr(self.config.synapse, "signing_key_ref", None) or None
        if ref is None:
            return None
        from pheasant.synapse.signing import resolve_signing_key_ref, sign_body

        seed = resolve_signing_key_ref(ref)
        return sign_body(body, seed)

    def _fleet_id(self) -> str | None:
        return getattr(self.config.synapse, "fleet_id", None) or None

    def _endpoint(self) -> str | None:
        return getattr(self.config.synapse, "endpoint", None) or None

    def _embedding_space(self) -> dict[str, Any]:
        settings = self.config.search.embeddings
        # enabled or not, report the configured model so the fleet pin is
        # legible even with a degenerate zero signature (disabled case).
        return {
            "model_id": settings.model,
            "dim": self._resolved_dimension(settings),
            "normalized": True,
        }

    def _resolved_dimension(self, settings: EmbeddingsSettings) -> int:
        """The vector width to publish. The schema's ``EmbeddingSpace.dim``
        is a required positive integer, but ``settings.dimensions`` is
        ``None`` by default (Synapse 21.4) so the provider applies the
        model's own native size instead of a guessed constant. Resolve a
        real number in priority order:

        1. An explicit ``settings.dimensions`` override — honored as-is.
        2. The width already on disk, straight from a real embedded vector —
           ground truth once anything has been synced.
        3. A small registry of known OpenAI-spec model native sizes, for a
           contract built before the first sync has embedded anything.
        4. The stub embedder's own default width, as a last resort for an
           unrecognized model — self-corrects the moment #2 applies.
        """
        if settings.dimensions:
            return int(settings.dimensions)
        all_vectors = getattr(self.vector_store, "all_vectors", None)
        if callable(all_vectors):
            try:
                sample = all_vectors()
            except Exception:
                sample = None
            if sample:
                return len(sample[0][1])
        return _KNOWN_MODEL_DIMENSIONS.get(settings.model, DEFAULT_STUB_DIMENSION)

    def _capabilities(self) -> dict[str, Any]:
        vector_on = self.config.search.embeddings.enabled
        search_modes = ["text", "graph", "hybrid"]
        if vector_on:
            search_modes.append("vector")
        # Synapse 25.4 (session A): advertise "image" when a source is
        # configured to ingest images (captioned into the same text space), so
        # the router's `--modality image` filter selects this region. Images
        # carry no modality-native contract vector — they are projected into
        # the one text embedding space (architecture §8).
        from pheasant.ingestion.captioner import config_has_image_source
        from pheasant.ingestion.transcriber import config_has_audio_source

        modalities = ["text", "code"]
        if config_has_image_source(self.config):
            modalities.append("image")
        # Synapse 25.4 (session B): advertise "audio" when a source is
        # configured to ingest audio (transcribed into the same text space), so
        # the router's `--modality audio` filter selects this region.
        if config_has_audio_source(self.config):
            modalities.append("audio")
        # Product 33.1: advertise "memory" when a `type: memory` source is
        # configured, so the router's `--modality memory` filter routes
        # remember/recall traffic to memory-capable regions. Existing wire
        # data — no schema bump (the 25.4 image/audio precedent).
        from pheasant.artifacts.store import memory_source

        if memory_source(self.config, getattr(self, "state", None)) is not None:
            modalities.append("memory")
        # Document extraction: advertise "document" when a source is configured
        # to ingest PDF/DOCX, so the router's `--modality document` filter
        # routes document questions to regions that can actually read them.
        # Existing wire data — no schema bump (the 25.4 image/audio precedent).
        from pheasant.ingestion.extractor import config_has_document_source

        if config_has_document_source(self.config):
            modalities.append("document")
        return {
            "search_modes": search_modes,
            "granularities": ["summaries", "chunks", "graph"],
            "modalities": modalities,
            # What *kinds* of source this region is built from — repository,
            # notion, slack, markdown_folder and so on. `modalities` says what
            # media the region can answer about; this says where the content
            # came from, which is the question a router gets asked ("who has
            # our Confluence?") and could not previously answer without
            # querying every region. Additive wire data: `capabilities` sets no
            # `additionalProperties`, so the vendored schema accepts it with no
            # bump and no re-vendor, and a router that ignores the key behaves
            # exactly as it did — the same call the 25.4 image/audio and 33.1
            # memory modality additions made.
            "source_types": sorted(
                {
                    getattr(source.type, "value", str(source.type))
                    for source in self.config.sources
                    if source.enabled
                }
            ),
            "returns_vectors": bool(vector_on),
            "auth": "none",
            "api_version": "v1",
        }

    def _signature(self, dim: int) -> dict[str, Any]:
        vectors = self._chunk_vectors(dim)
        if vectors is None or vectors.shape[0] == 0:
            return _degenerate_signature(dim)
        return _signature_from_vectors(vectors, max_clusters=MAX_CLUSTERS, seed=self.seed)

    def _chunk_vectors(self, dim: int) -> np.ndarray | None:
        """Stack the region's chunk vectors from the vector store, or None."""

        if self.vector_store is None or not self.config.search.embeddings.enabled:
            return None
        rows = sorted(self._iter_store_vectors(), key=lambda pair: pair[0])
        if not rows:
            return None
        matrix = np.asarray([vec for _, vec in rows], dtype=np.float64)
        if matrix.ndim != 2 or matrix.shape[1] != dim:
            logger.warning(
                "Vector store dim %s != embedding dim %s; emitting degenerate signature",
                None if matrix.ndim != 2 else matrix.shape[1],
                dim,
            )
            return None
        return matrix

    def _iter_store_vectors(self) -> list[tuple[str, list[float]]]:
        """Read all (chunk_id, vector) pairs the store exposes.

        Both shipped backends (``NumpyVectorStore`` / ``LanceDBVectorStore``)
        provide an additive ``all_vectors()`` bulk reader. A backend without
        one degrades gracefully to the degenerate signature (the contract still
        carries vocabulary + watermark).
        """

        reader = getattr(self.vector_store, "all_vectors", None)
        if callable(reader):
            return list(reader())
        logger.info(
            "Vector store %s exposes no bulk vector reader; signature uses the "
            "degenerate fallback (contract still carries vocabulary + watermark)",
            type(self.vector_store).__name__,
        )
        return []

    def _vocabulary(self) -> dict[str, Any]:
        """What this region is about, for the router to score against.

        Sourced from the FTS index's own term/document-frequency table rather
        than from concept-extraction nodes, which were retired (see
        ``graph.enrichment._add_concept``). **The wire format is unchanged** —
        same ``top_concepts`` shape, same ``weight`` scale, same minhash over
        the same kind of term set — so the vendored schema, the fixtures and
        the router's scoring are all untouched. Only where the terms come
        from changed, and they now come from what is actually searchable in
        this region instead of a derived layer that duplicated it.

        Weights are document frequencies normalized to (0, 1] against the most
        common term, keeping them comparable across regions of different sizes
        the way the summed concept weights were meant to be.
        """
        from pheasant.search.sqlite_store import corpus_vocabulary

        vocabulary = corpus_vocabulary(self.state, MAX_VOCAB_TERMS)
        top = float(vocabulary[0][1]) if vocabulary else 0.0
        top_concepts = [
            {"term": term, "weight": round(count / top, 6) if top else 0.0}
            for term, count in vocabulary
        ]
        terms = sorted({concept["term"] for concept in top_concepts})
        minhash = _encode_u64_b64(_minhash_signature(terms)) if terms else None
        return {
            "top_concepts": top_concepts,
            "languages": ["en"],
            "minhash": minhash,
        }

    def _watermark(self, generated_at: str) -> dict[str, Any]:
        digest = self._source_checkpoints_digest()
        counts = self._counts()
        return {
            "last_sync_completed_at": generated_at,
            "source_checkpoints_digest": digest,
            "artifact_count": counts["artifact_count"],
            "chunk_count": counts["chunk_count"],
            "bytes_indexed": counts["bytes_indexed"],
        }

    def _source_checkpoints_digest(self) -> str:
        """sha256 over each source's ordered content fingerprint.

        Derived from the per-artifact ``(source_id, relative_path, sha256)``
        triples in the artifacts table — the true content identity of the
        region — *not* the connector checkpoints, whose ``high_watermark``
        carries transient per-sync counters (indexed/skipped) that would make
        two no-change syncs disagree. Two syncs with unchanged content
        therefore always produce the identical digest.
        """

        rows = self.state.rows(
            """SELECT source_id, relative_path, sha256
               FROM artifacts
               ORDER BY source_id ASC, relative_path ASC"""
        )
        fingerprint = [
            [str(row["source_id"]), str(row["relative_path"] or ""), str(row["sha256"] or "")]
            for row in rows
        ]
        canonical = json.dumps(
            fingerprint, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _counts(self) -> dict[str, int]:
        artifact_count = int(self.state.rows("SELECT COUNT(*) AS c FROM artifacts")[0]["c"])
        chunk_count = int(self.state.rows("SELECT COUNT(*) AS c FROM chunks")[0]["c"])
        bytes_rows = self.state.rows("SELECT COALESCE(SUM(size_bytes), 0) AS b FROM artifacts")
        bytes_indexed = int(bytes_rows[0]["b"] or 0)
        return {
            "artifact_count": artifact_count,
            "chunk_count": chunk_count,
            "bytes_indexed": bytes_indexed,
        }
