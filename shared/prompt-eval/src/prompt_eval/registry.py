"""Durable coordinator for serialized campaigns; keep this DB across all runs.

SQLite serializes transitions, not model execution. A crash leaves a run active:
fail closed and investigate rather than releasing a potentially revealed holdout.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .models import now
from .security import canonical_bytes, digest, traced


class RegistryError(ValueError):
    pass


def manifest_digest(config):
    return digest(
        [c.model_dump(mode="json") for c in sorted(config.cases, key=lambda c: c.case_id)]
    )


def holdout_digest(config):
    return digest(
        [
            {"corpus": c.corpus, "source_digests": c.source_digests}
            for c in sorted(config.cases, key=lambda c: c.case_id)
            if c.split == "holdout"
        ]
    )


def holdout_sources(config):
    # IDs, corpus labels, ordering and digest-key aliases do not change source content.
    return {digest(sorted(c.source_digests.values())) for c in config.cases if c.split == "holdout"}


class Registry:
    @traced
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS datasets (
                    digest TEXT PRIMARY KEY, version TEXT NOT NULL, manifest TEXT NOT NULL,
                    holdout TEXT NOT NULL, release TEXT, revealed_at TEXT);
                CREATE TABLE IF NOT EXISTS revealed_sources (
                    fingerprint TEXT PRIMARY KEY, dataset TEXT NOT NULL, revealed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS versions (
                    experiment TEXT, version TEXT, digest TEXT,
                    PRIMARY KEY(experiment, version));
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, dataset TEXT NOT NULL, active INTEGER NOT NULL,
                    config TEXT NOT NULL);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @traced
    def reserve(self, run_id, config):
        with self.connect() as db:
            row = db.execute(
                "SELECT digest FROM versions WHERE experiment=? AND version=?",
                (config.experiment_id, config.dataset.version),
            ).fetchone()
            if row and row[0] != config.dataset.digest:
                raise RegistryError("Dataset version already names different content")
            row = db.execute(
                "SELECT manifest,release,revealed_at FROM datasets WHERE digest=?",
                (config.dataset.digest,),
            ).fetchone()
            if row and row[0] != manifest_digest(config):
                raise RegistryError("Dataset digest cannot name a changed split manifest")
            if row and (row[2] or (row[1] and config.split != "holdout")):
                raise RegistryError(
                    "Dataset is frozen or revealed; a new tuning cycle requires new data and holdout"
                )
            if db.execute(
                "SELECT 1 FROM datasets WHERE holdout=? AND revealed_at IS NOT NULL",
                (holdout_digest(config),),
            ).fetchone():
                raise RegistryError("Previously revealed holdout cannot be reused")
            if any(
                db.execute(
                    "SELECT 1 FROM revealed_sources WHERE fingerprint=?", (source,)
                ).fetchone()
                for source in holdout_sources(config)
            ):
                raise RegistryError("Previously revealed holdout source cannot be reused")
            if db.execute(
                "SELECT 1 FROM runs WHERE dataset=? AND active=1", (config.dataset.digest,)
            ).fetchone():
                raise RegistryError("Another run owns this dataset; serialize campaign execution")
            try:
                db.execute(
                    "INSERT OR IGNORE INTO datasets VALUES (?,?,?,?,NULL,NULL)",
                    (
                        config.dataset.digest,
                        config.dataset.version,
                        manifest_digest(config),
                        holdout_digest(config),
                    ),
                )
                db.execute(
                    "INSERT OR IGNORE INTO versions VALUES (?,?,?)",
                    (config.experiment_id, config.dataset.version, config.dataset.digest),
                )
                db.execute(
                    "INSERT INTO runs VALUES (?,?,1,?)",
                    (run_id, config.dataset.digest, digest(config)),
                )
            except sqlite3.IntegrityError as exc:
                raise RegistryError("Run IDs cannot be reused") from exc

    @traced
    def finish(self, run_id):
        with self.connect() as db:
            db.execute("UPDATE runs SET active=0 WHERE id=?", (run_id,))

    @traced
    def release(self, release, config):
        if release.dataset != config.dataset or release.manifest_digest != manifest_digest(config):
            raise RegistryError("Release dataset does not match manifest")
        if not any(c.split == "holdout" for c in config.cases):
            raise RegistryError("Release requires sealed holdout cases")
        with self.connect() as db:
            if db.execute(
                "SELECT 1 FROM runs WHERE dataset=? AND active=1", (config.dataset.digest,)
            ).fetchone():
                raise RegistryError("Cannot release while a run is active")
            row = db.execute(
                "SELECT release,revealed_at,manifest FROM datasets WHERE digest=?",
                (config.dataset.digest,),
            ).fetchone()
            if not row or row[0] or row[1] or row[2] != release.manifest_digest:
                raise RegistryError("Dataset is unknown, already released, or changed")
            db.execute(
                "UPDATE datasets SET release=? WHERE digest=?",
                (canonical_bytes(release).decode(), config.dataset.digest),
            )

    @traced
    def reveal(self, config, release, run_id):
        from .models import Release

        if config.split != "holdout" or release is None:
            raise RegistryError("Holdout requires a frozen release record")
        required = (
            release.experiment_id == config.experiment_id,
            release.dataset == config.dataset,
            release.manifest_digest == manifest_digest(config),
            release.production_behavior == config.production_behavior,
            release.stages == config.stages,
            release.model == config.model,
            release.repetitions == config.repetitions,
            set(release.baseline_prompts) == {s.name for s in config.stages},
        )
        if not all(required):
            raise RegistryError("Holdout configuration differs from frozen release")
        with self.connect() as db:
            owner = db.execute(
                "SELECT config FROM runs WHERE id=? AND dataset=? AND active=1",
                (run_id, config.dataset.digest),
            ).fetchone()
            if not owner or owner[0] != digest(config):
                raise RegistryError("Reveal requires the reserved run owner")
            row = db.execute(
                "SELECT release,revealed_at FROM datasets WHERE digest=?", (config.dataset.digest,)
            ).fetchone()
            if not row or not row[0] or row[1] or Release.model_validate_json(row[0]) != release:
                raise RegistryError("Release is missing, altered, or already revealed")
            if any(
                db.execute(
                    "SELECT 1 FROM revealed_sources WHERE fingerprint=?", (source,)
                ).fetchone()
                for source in holdout_sources(config)
            ):
                raise RegistryError("Previously revealed holdout source cannot be reused")
            stamp = now()
            db.executemany(
                "INSERT INTO revealed_sources VALUES (?,?,?)",
                [(source, config.dataset.digest, stamp) for source in holdout_sources(config)],
            )
            db.execute(
                "UPDATE datasets SET revealed_at=? WHERE digest=?", (stamp, config.dataset.digest)
            )
            return stamp
