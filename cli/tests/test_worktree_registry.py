from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock

import pytest

from awf.worktrees.models import (
    CommandResult,
    DeploymentState,
    Lease,
    LeaseState,
    PromotionMode,
    PromotionSource,
    Purpose,
    ReleaseBridge,
    ReleaseSource,
    ReleaseState,
    ResolutionState,
)
from awf.worktrees.registry import WorktreeRegistry


def lease(
    path: Path,
    *,
    initiative: str = "reward-widget",
    worktree_name: str | None = None,
) -> Lease:
    return Lease.new(
        repository_id="repo-1",
        repository_name="demo",
        repository_root=path / "repo",
        worktree_path=path / "cache" / (worktree_name or initiative),
        initiative=initiative,
        purpose=Purpose.FEATURE,
        branch=f"awf/{initiative}/feature",
        base_ref="origin/staging",
        head_sha="a" * 40,
        managed=True,
        owner_kind="awf",
        promotion_mode=PromotionMode.EXACT,
        resolution_state=ResolutionState.NONE,
        source_base_sha=None,
        source_head_sha=None,
        target_base_sha=None,
        reviewed_paths=(),
        conflicted_paths=(),
    )


def release_bridge(tmp_path: Path, registry: WorktreeRegistry) -> ReleaseBridge:
    promotion = Lease.new(
        repository_id="repo-1",
        repository_name="demo",
        repository_root=tmp_path / "repo",
        worktree_path=tmp_path / "cache" / "release",
        initiative="release-august-hotfix",
        purpose=Purpose.PROMOTE,
        branch="awf/release-august-hotfix/promote",
        base_ref="origin/main",
        head_sha="a" * 40,
        managed=True,
        owner_kind="awf",
    )
    registry.create_lease(promotion)
    return registry.create_release(
        ReleaseBridge.new(
            repository_id=promotion.repository_id,
            repository_name=promotion.repository_name,
            repository_root=promotion.repository_root,
            release_id="august-hotfix",
            target_branch="main",
            lease_id=promotion.id,
        )
    )


def release_source(bridge: ReleaseBridge, ordinal: int, source_pr: int) -> ReleaseSource:
    return ReleaseSource(
        bridge_id=bridge.id,
        ordinal=ordinal,
        source_pr=source_pr,
        base_ref="staging",
        base_sha="a" * 40,
        head_sha="b" * 40,
        merge_sha="c" * 40,
        changed_paths=("src/release.py",),
    )


def promotion_source(
    promotion: Lease, ordinal: int, source_pr: int
) -> PromotionSource:
    return PromotionSource(
        lease_id=promotion.id,
        ordinal=ordinal,
        source_pr=source_pr,
        base_ref="staging",
        base_sha=chr(ord("a") + ordinal) * 40,
        head_sha=chr(ord("b") + ordinal) * 40,
        merge_sha=chr(ord("c") + ordinal) * 40,
        changed_paths=(f"src/source-{source_pr}.py",),
    )


def test_registry_creates_and_round_trips_a_lease(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))

    loaded = registry.get_lease(created.id)

    assert loaded == created
    assert loaded.state is LeaseState.ACTIVE
    assert loaded.deployment_state is DeploymentState.NOT_REQUIRED


def test_registry_persists_ordered_immutable_release_sources_with_cas(
    tmp_path: Path,
) -> None:
    registry = WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3")
    created = release_bridge(tmp_path, registry)

    source = release_source(created, 0, 372)
    staged = registry.stage_release_source(
        source,
        expected_version=created.version,
    )
    promotion = registry.get_lease(created.lease_id)
    assert promotion is not None
    promotion, first = registry.accept_release_source(
        source,
        expected_release_version=staged.version,
        lease_id=promotion.id,
        expected_lease_version=promotion.version,
        head_sha="d" * 40,
        target_base_sha="e" * 40,
    )

    assert first.state is ReleaseState.OPEN
    assert first.version == created.version + 2
    assert first.source_digest != created.source_digest
    assert promotion.head_sha == "d" * 40
    assert registry.find_release("repo-1", "august-hotfix") == first
    assert first.to_dict()["sources"] == [
        {
            "ordinal": 0,
            "source_pr": 372,
            "base_ref": "staging",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "merge_sha": "c" * 40,
            "changed_paths": ["src/release.py"],
        }
    ]

    with pytest.raises(ValueError, match="already pinned"):
        registry.stage_release_source(
            release_source(first, 1, 372),
            expected_version=first.version,
        )
    with pytest.raises(ValueError, match="ordinal must append"):
        registry.stage_release_source(
            release_source(first, 2, 373),
            expected_version=first.version,
        )
    with pytest.raises(RuntimeError, match="changed concurrently"):
        registry.stage_release_source(
            release_source(first, 1, 373),
            expected_version=created.version,
        )

    sealed = registry.transition_release(
        first.id,
        ReleaseState.SEALED,
        expected_version=first.version,
        last_verified_target_sha="d" * 40,
    )
    assert sealed.state is ReleaseState.SEALED
    assert sealed.last_verified_target_sha == "d" * 40
    with pytest.raises(ValueError, match="not open for sources"):
        registry.stage_release_source(
            release_source(sealed, 1, 373),
            expected_version=sealed.version,
        )


def test_registry_blocks_seal_while_source_add_is_pending(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3")
    created = release_bridge(tmp_path, registry)
    source = release_source(created, 0, 372)
    staged = registry.stage_release_source(
        source,
        expected_version=created.version,
    )

    with pytest.raises(ValueError, match="pending source"):
        registry.transition_release(
            staged.id,
            ReleaseState.SEALED,
            expected_version=staged.version,
            last_verified_target_sha="d" * 40,
        )


def test_registry_publishes_release_and_lease_atomically(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3")
    created = release_bridge(tmp_path, registry)
    source = release_source(created, 0, 372)
    staged = registry.stage_release_source(
        source,
        expected_version=created.version,
    )
    promotion = registry.get_lease(created.lease_id)
    assert promotion is not None
    promotion, accepted = registry.accept_release_source(
        source,
        expected_release_version=staged.version,
        lease_id=promotion.id,
        expected_lease_version=promotion.version,
        head_sha="d" * 40,
        target_base_sha="e" * 40,
    )
    sealed = registry.transition_release(
        accepted.id,
        ReleaseState.SEALED,
        expected_version=accepted.version,
        last_verified_target_sha="e" * 40,
    )

    with pytest.raises(RuntimeError, match="changed concurrently"):
        registry.publish_release(
            sealed.id,
            promotion.id,
            expected_release_version=accepted.version,
            expected_lease_version=promotion.version,
            target_pr=900,
            head_sha=promotion.head_sha,
            target_base_sha="e" * 40,
        )
    assert registry.get_lease(promotion.id).state is LeaseState.ACTIVE
    assert registry.get_release(sealed.id).state is ReleaseState.SEALED

    with pytest.raises(RuntimeError, match="changed concurrently"):
        registry.publish_release(
            sealed.id,
            promotion.id,
            expected_release_version=sealed.version,
            expected_lease_version=promotion.version - 1,
            target_pr=900,
            head_sha=promotion.head_sha,
            target_base_sha="e" * 40,
        )
    assert registry.get_lease(promotion.id).state is LeaseState.ACTIVE
    assert registry.get_release(sealed.id).state is ReleaseState.SEALED

    published_lease, published = registry.publish_release(
        sealed.id,
        promotion.id,
        expected_release_version=sealed.version,
        expected_lease_version=promotion.version,
        target_pr=900,
        head_sha=promotion.head_sha,
        target_base_sha="e" * 40,
    )
    assert published_lease.state is LeaseState.PR_OPEN
    assert published_lease.target_pr == 900
    assert published.state is ReleaseState.PUBLISHED
    assert published.target_pr == 900





def test_registry_atomically_round_trips_ordered_promotion_source_pins(
    tmp_path: Path,
) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    promotion = replace(
        lease(tmp_path),
        initiative="prs-372-373-to-main-out-of-order",
        worktree_path=tmp_path / "cache" / "promotion",
        purpose=Purpose.PROMOTE,
        branch="awf/prs-372-373-to-main-out-of-order/promote",
        base_ref="origin/main",
        promotion_mode=PromotionMode.OUT_OF_ORDER,
        source_pr=372,
        source_base_sha="a" * 40,
        source_head_sha="d" * 40,
        target_base_sha="e" * 40,
        reviewed_paths=("src/source-372.py", "src/source-373.py"),
    )
    sources = (
        promotion_source(promotion, 0, 372),
        promotion_source(promotion, 1, 373),
    )

    created = registry.create_promotion_lease(promotion, sources)

    assert registry.get_lease(created.id) == created
    assert registry.get_promotion_sources(created.id) == sources
    assert registry.get_promotion_sources_read_only(created.id) == sources
    invalid_promotion = replace(
        promotion,
        id="another-promotion",
        initiative="another-promotion",
        worktree_path=tmp_path / "cache" / "another-promotion",
        branch="awf/another-promotion/promote",
    )
    with pytest.raises(ValueError, match="invalid ordered"):
        registry.create_promotion_lease(
            invalid_promotion,
            (
                promotion_source(promotion, 0, 372),
                promotion_source(promotion, 1, 372),
            ),
        )
    assert registry.get_lease(invalid_promotion.id) is None


def test_registry_backfills_verified_legacy_single_source_pin(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    legacy = replace(
        lease(tmp_path),
        initiative="legacy-out-of-order",
        worktree_path=tmp_path / "cache" / "legacy-out-of-order",
        purpose=Purpose.PROMOTE,
        branch="awf/legacy-out-of-order/promote",
        base_ref="origin/main",
        promotion_mode=PromotionMode.OUT_OF_ORDER,
        source_pr=372,
        source_base_sha="a" * 40,
        source_head_sha="b" * 40,
        target_base_sha="e" * 40,
        reviewed_paths=("src/source-372.py",),
    )
    source = promotion_source(legacy, 0, 372)
    registry.create_lease(legacy)

    assert registry.get_promotion_sources(legacy.id) == ()
    assert registry.backfill_promotion_sources(legacy, (source,)) == (source,)
    assert registry.get_promotion_sources(legacy.id) == (source,)


def test_registry_round_trips_out_of_order_provenance(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = replace(
        lease(tmp_path),
        promotion_mode=PromotionMode.OUT_OF_ORDER,
        resolution_state=ResolutionState.PENDING,
        source_base_sha="a" * 40,
        source_head_sha="b" * 40,
        target_base_sha="c" * 40,
        reviewed_paths=("src/a.py", "src/b.py"),
        conflicted_paths=("src/b.py",),
        protected_index_entries=(("src/a.py", ("100644", "d" * 40)),),
    )

    registry.create_lease(created)

    assert registry.get_lease(created.id) == created
    payload = created.to_dict()
    assert {
        "promotion_mode": payload["promotion_mode"],
        "resolution_state": payload["resolution_state"],
        "source_base_sha": payload["source_base_sha"],
        "source_head_sha": payload["source_head_sha"],
        "target_base_sha": payload["target_base_sha"],
        "reviewed_paths": payload["reviewed_paths"],
        "conflicted_paths": payload["conflicted_paths"],
        "conflict_source_ordinal": payload["conflict_source_ordinal"],
        "legacy_source_trailers": payload["legacy_source_trailers"],
        "protected_index_entries": payload["protected_index_entries"],
    } == {
        "promotion_mode": "out_of_order",
        "resolution_state": "pending",
        "source_base_sha": "a" * 40,
        "source_head_sha": "b" * 40,
        "target_base_sha": "c" * 40,
        "reviewed_paths": ["src/a.py", "src/b.py"],
        "conflicted_paths": ["src/b.py"],
        "conflict_source_ordinal": None,
        "legacy_source_trailers": False,
        "protected_index_entries": [
            {"path": "src/a.py", "mode": "100644", "blob_oid": "d" * 40}
        ],
    }


@pytest.mark.parametrize("mode", ("120000", "160000"))
def test_registry_round_trips_supported_protected_index_entry_modes(
    tmp_path: Path,
    mode: str,
) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = replace(
        lease(tmp_path),
        promotion_mode=PromotionMode.OUT_OF_ORDER,
        protected_index_entries=(("entry", (mode, "d" * 40)),),
    )

    registry.create_lease(created)

    assert registry.get_lease(created.id) == created

def test_registry_migrates_legacy_lease_with_promotion_defaults(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "worktrees.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE worktree_leases (
                id TEXT PRIMARY KEY,
                repository_id TEXT NOT NULL,
                repository_name TEXT NOT NULL,
                repository_root TEXT NOT NULL,
                worktree_path TEXT NOT NULL UNIQUE,
                initiative TEXT NOT NULL,
                purpose TEXT NOT NULL,
                branch TEXT NOT NULL,
                base_ref TEXT NOT NULL,
                head_sha TEXT NOT NULL,
                managed INTEGER NOT NULL,
                owner_kind TEXT NOT NULL,
                owner_id TEXT,
                state TEXT NOT NULL,
                source_pr INTEGER,
                target_pr INTEGER,
                deployment_state TEXT NOT NULL,
                retain INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                removed_at TEXT,
                version INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE worktree_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lease_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT,
                observed_head_sha TEXT,
                pr_number INTEGER,
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO worktree_leases (
                id, repository_id, repository_name, repository_root, worktree_path,
                initiative, purpose, branch, base_ref, head_sha, managed, owner_kind,
                owner_id, state, source_pr, target_pr, deployment_state, retain,
                created_at, last_used_at, updated_at, removed_at, version
            ) VALUES (
                :id, :repository_id, :repository_name, :repository_root, :worktree_path,
                :initiative, :purpose, :branch, :base_ref, :head_sha, :managed,
                :owner_kind, :owner_id, :state, :source_pr, :target_pr,
                :deployment_state, :retain, :created_at, :last_used_at, :updated_at,
                :removed_at, :version
            )
            """,
            {
                "id": "legacy-lease",
                "repository_id": "repo-1",
                "repository_name": "demo",
                "repository_root": str(tmp_path / "repo"),
                "worktree_path": str(tmp_path / "cache" / "legacy"),
                "initiative": "legacy",
                "purpose": "feature",
                "branch": "awf/legacy/feature",
                "base_ref": "origin/staging",
                "head_sha": "a" * 40,
                "managed": 1,
                "owner_kind": "awf",
                "owner_id": None,
                "state": "ACTIVE",
                "source_pr": None,
                "target_pr": None,
                "deployment_state": "not_required",
                "retain": 0,
                "created_at": "2030-01-02T03:04:05+00:00",
                "last_used_at": "2030-01-02T03:04:05+00:00",
                "updated_at": "2030-01-02T03:04:05+00:00",
                "removed_at": None,
                "version": 0,
            },
        )
        connection.execute(
            """
            INSERT INTO worktree_events (
                lease_id, event_type, from_state, to_state, observed_head_sha,
                pr_number, summary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-lease",
                "legacy",
                "ACTIVE",
                "ACTIVE",
                None,
                None,
                "legacy event",
                "2030-01-02T03:04:05+00:00",
            ),
        )

    registry = WorktreeRegistry(db_path)
    registry.ensure()

    loaded = registry.get_lease("legacy-lease")

    assert loaded is not None
    assert loaded.promotion_mode is PromotionMode.EXACT
    assert loaded.resolution_state is ResolutionState.NONE
    assert loaded.source_base_sha is None
    assert loaded.source_head_sha is None
    assert loaded.target_base_sha is None
    assert loaded.reviewed_paths == ()
    assert loaded.conflicted_paths == ()
    assert registry.get_promotion_sources("legacy-lease") == ()
    assert loaded.conflict_source_ordinal is None
    assert loaded.legacy_source_trailers is False
    events = registry.list_events("legacy-lease")
    assert events[-1].evidence is None


def test_transition_updates_resolution_metadata_with_compare_and_swap(
    tmp_path: Path,
) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(
        replace(
            lease(tmp_path),
            promotion_mode=PromotionMode.OUT_OF_ORDER,
            resolution_state=ResolutionState.PENDING,
            conflicted_paths=("src/a.py",),
            protected_index_entries=(("src/a.py", ("100644", "d" * 40)),),
        )
    )

    updated = registry.transition(
        created.id,
        LeaseState.ACTIVE,
        expected_version=created.version,
        resolution_state=ResolutionState.MANUAL_REVIEWED,
        conflicted_paths=(),
        conflict_source_ordinal=1,
        protected_index_entries=(("src/a.py", None),),
    )

    assert updated.resolution_state is ResolutionState.MANUAL_REVIEWED
    assert updated.conflicted_paths == ()
    assert updated.conflict_source_ordinal == 1
    assert updated.protected_index_entries == (("src/a.py", None),)
    assert registry.get_lease(created.id) == updated
    cleared = registry.transition(
        updated.id,
        LeaseState.BLOCKED,
        expected_version=updated.version,
        clear_conflict_source_ordinal=True,
    )
    assert cleared.conflict_source_ordinal is None


@pytest.mark.parametrize(
    ("field", "metadata"),
    [
        ("reviewed_paths", "not-json"),
        ("reviewed_paths", '["src/b.py","src/a.py"]'),
        ("reviewed_paths", '["src/a.py","src/a.py"]'),
        ("conflicted_paths", '["src/a.py",1]'),
    ],
)
def test_registry_rejects_invalid_path_metadata(
    tmp_path: Path, field: str, metadata: str
) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))
    with sqlite3.connect(registry.db_path) as connection:
        connection.execute(
            f"UPDATE worktree_leases SET {field} = ? WHERE id = ?",
            (metadata, created.id),
        )

    with pytest.raises(ValueError, match=field):
        registry.get_lease(created.id)


def test_registry_rejects_blob_path_metadata(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))
    with sqlite3.connect(registry.db_path) as connection:
        connection.execute(
            "UPDATE worktree_leases SET reviewed_paths = ? WHERE id = ?",
            (sqlite3.Binary(b'["src/a.py"]'), created.id),
        )

    with pytest.raises(ValueError, match="reviewed_paths"):
        registry.get_lease(created.id)


@pytest.mark.parametrize(
    "name",
    [
        "promotion_mode",
        "resolution_state",
        "source_base_sha",
        "source_head_sha",
        "target_base_sha",
        "reviewed_paths",
        "conflicted_paths",
    ],
)
def test_registry_rejects_promotion_metadata_filters(tmp_path: Path, name: str) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")

    with pytest.raises(ValueError, match="unsupported lease filter"):
        registry.list_leases(**{name: None})


def test_ensure_resumes_and_repeats_a_partial_legacy_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "worktrees.sqlite3"
    _create_partial_legacy_lease_table(db_path)
    registry = WorktreeRegistry(db_path)

    registry.ensure()
    registry.ensure()

    with sqlite3.connect(db_path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(worktree_leases)")
        }
    assert {
        "promotion_mode",
        "resolution_state",
        "source_base_sha",
        "source_head_sha",
        "target_base_sha",
        "reviewed_paths",
        "conflicted_paths",
        "protected_index_entries",
        "legacy_source_trailers",
        "conflict_source_ordinal",
    } <= columns


def test_ensure_migrates_legacy_schema_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "worktrees.sqlite3"
    _create_partial_legacy_lease_table(db_path)
    registry = WorktreeRegistry(db_path)
    actual_connect = registry._connect
    gate = EnsureMigrationGate()

    def connect() -> PausingMigrationConnection:
        return PausingMigrationConnection(actual_connect(), gate)

    monkeypatch.setattr(registry, "_connect", connect)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(registry.ensure)
        assert gate.first_pragma.wait(timeout=1)
        second = executor.submit(registry.ensure)
        try:
            assert not gate.second_pragma.wait(timeout=1)
        finally:
            gate.release.set()
        first.result()
        second.result()


def test_registry_rejects_two_active_leases_for_same_identity(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    registry.create_lease(lease(tmp_path))

    with pytest.raises(ValueError, match="active lease already exists"):
        registry.create_lease(lease(tmp_path))


def test_removed_lease_does_not_block_a_replacement(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    first = registry.create_lease(lease(tmp_path))
    registry.transition(first.id, LeaseState.REMOVED, expected_version=first.version)

    replacement = lease(tmp_path, worktree_name="reward-widget-replacement")
    second = registry.create_lease(replacement)

    assert second.id != first.id
    assert second.worktree_path == replacement.worktree_path
    with pytest.raises(
        sqlite3.IntegrityError, match="worktree_leases.worktree_path"
    ):
        registry.create_lease(
            lease(
                tmp_path,
                initiative="another-initiative",
                worktree_name="reward-widget",
            )
        )


def test_transition_is_compare_and_swap_and_appends_event(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))

    updated = registry.transition(
        created.id,
        LeaseState.PR_OPEN,
        expected_version=created.version,
        summary="target PR opened",
        pr_number=42,
    )

    assert updated.version == created.version + 1
    assert updated.target_pr == 42
    assert registry.list_events(created.id)[-1].to_state is LeaseState.PR_OPEN
    with pytest.raises(RuntimeError, match="lease changed concurrently"):
        registry.transition(created.id, LeaseState.MERGED, expected_version=created.version)



def test_cleanup_reservation_is_cas_guarded_and_completes_removal(
    tmp_path: Path,
) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))

    reservation = registry.reserve_cleanup(
        created.id,
        expected_version=created.version,
        branch_sha="a" * 40,
    )

    assert reservation.lease_id == created.id
    assert reservation.reserved_version == created.version + 1
    with pytest.raises(RuntimeError, match="cleanup is reserved"):
        registry.transition(
            created.id,
            LeaseState.PR_OPEN,
            expected_version=reservation.reserved_version,
        )
    removed = registry.complete_cleanup(
        created.id, expected_version=reservation.reserved_version
    )

    assert removed.state is LeaseState.REMOVED
    assert registry.get_cleanup_reservation(created.id) is None


def test_releasing_cleanup_reservation_allows_later_refresh_transition(
    tmp_path: Path,
) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))
    reservation = registry.reserve_cleanup(
        created.id,
        expected_version=created.version,
        branch_sha="a" * 40,
    )

    registry.release_cleanup_reservation(
        created.id, expected_version=reservation.reserved_version
    )
    refreshed = registry.transition(
        created.id,
        LeaseState.PR_OPEN,
        expected_version=reservation.reserved_version + 1,
    )

    assert refreshed.state is LeaseState.PR_OPEN


def test_cleanup_warning_event_preserves_the_reservation_version(
    tmp_path: Path,
) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))
    reservation = registry.reserve_cleanup(
        created.id,
        expected_version=created.version,
        branch_sha="a" * 40,
    )

    registry.record_cleanup_event(
        created.id, event_type="remote_branch_cleanup_failed", summary="branch changed"
    )

    assert registry.get_lease(created.id).version == reservation.reserved_version
    assert registry.list_events(created.id)[-1].event_type == "remote_branch_cleanup_failed"

def test_event_summary_is_bounded_to_512_utf8_bytes(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))

    registry.transition(
        created.id,
        LeaseState.PR_OPEN,
        expected_version=created.version,
        summary="한" * 300,
    )

    stored = registry.list_events(created.id)[-1].summary
    assert len(stored.encode("utf-8")) <= 512
    assert stored == "한" * 170


def test_touch_updates_only_usage_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))
    timestamp = "2030-01-02T03:04:05+00:00"
    monkeypatch.setattr("awf.worktrees.registry.now_iso", lambda: timestamp)

    updated = registry.touch(
        created.id,
        expected_version=created.version,
        head_sha="b" * 40,
    )

    expected = created.to_dict()
    expected.update(
        head_sha="b" * 40,
        last_used_at=timestamp,
        updated_at=timestamp,
        version=created.version + 1,
    )
    assert updated.to_dict() == expected


def test_touch_rejects_a_stale_version(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))
    registry.touch(created.id, expected_version=created.version, head_sha="b" * 40)

    with pytest.raises(RuntimeError, match="lease changed concurrently"):
        registry.touch(created.id, expected_version=created.version, head_sha="c" * 40)


def test_registry_closes_each_sqlite_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    actual_connect = registry._connect
    connections: list[TrackingConnection] = []

    def connect() -> TrackingConnection:
        tracked = TrackingConnection(actual_connect())
        connections.append(tracked)
        return tracked

    monkeypatch.setattr(registry, "_connect", connect)
    registry.ensure()
    created = registry.create_lease(lease(tmp_path))

    assert registry.get_lease(created.id) == created
    assert registry.find_active(
        created.repository_id, created.initiative, created.purpose
    ) == created
    assert registry.list_leases() == [created]
    assert registry.list_events(created.id) == []
    assert connections
    assert all(connection.closed for connection in connections)


class TrackingConnection:
    def __init__(self, connection: object) -> None:
        self.connection = connection
        self.closed = False

    def __enter__(self) -> "TrackingConnection":
        self.connection.__enter__()
        return self

    def __exit__(self, *args: object) -> bool | None:
        return self.connection.__exit__(*args)

    def close(self) -> None:
        self.closed = True
        self.connection.close()

    def __getattr__(self, name: str) -> object:
        return getattr(self.connection, name)


def _create_partial_legacy_lease_table(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE worktree_leases (
                id TEXT PRIMARY KEY,
                repository_id TEXT NOT NULL,
                initiative TEXT NOT NULL,
                purpose TEXT NOT NULL,
                state TEXT NOT NULL,
                promotion_mode TEXT NOT NULL DEFAULT 'exact'
            );
            """
        )


class EnsureMigrationGate:
    def __init__(self) -> None:
        self.first_pragma = Event()
        self.second_pragma = Event()
        self.release = Event()
        self._lock = Lock()
        self._pragma_count = 0

    def pause_after_pragma(self) -> None:
        with self._lock:
            self._pragma_count += 1
            pragma_count = self._pragma_count
        if pragma_count == 1:
            self.first_pragma.set()
        elif pragma_count == 2:
            self.second_pragma.set()
        else:
            return
        if not self.release.wait(timeout=5):
            raise RuntimeError("migration gate timed out")


class PausingMigrationConnection:
    def __init__(
        self, connection: sqlite3.Connection, gate: EnsureMigrationGate
    ) -> None:
        self.connection = connection
        self.gate = gate

    def __enter__(self) -> "PausingMigrationConnection":
        self.connection.__enter__()
        return self

    def __exit__(self, *args: object) -> bool | None:
        return self.connection.__exit__(*args)

    def close(self) -> None:
        self.connection.close()

    def execute(self, statement: str, parameters: object = ()) -> object:
        result = self.connection.execute(statement, parameters)
        if statement == "PRAGMA table_info(worktree_leases)":
            self.gate.pause_after_pragma()
        return result

    def __getattr__(self, name: str) -> object:
        return getattr(self.connection, name)


def test_command_result_has_versioned_json_envelope() -> None:
    result = CommandResult.ok("wt.status", decision="no_op")

    payload = result.to_dict()

    assert payload["schema_version"] == 1
    assert payload["command"] == "wt.status"
    assert payload["status"] == "ok"
    assert payload["decision"] == "no_op"
    assert payload["actions"] == []
    assert payload["blockers"] == []
    assert payload["warnings"] == []


def test_command_result_external_error_uses_external_failure_exit_code() -> None:
    result = CommandResult.external_error(
        "wt.promote",
        code="github_unavailable",
        message="gh authentication failed",
    )

    payload = result.to_dict()

    assert payload["status"] == "error"
    assert payload["decision"] == "blocked"
    assert payload["exit_code"] == 4
    assert payload["blockers"] == [
        {"code": "github_unavailable", "message": "gh authentication failed"}
    ]
