"""Phase 8 migration tests against a copy of the real database.

The migration rebuilds `analytics_snapshot` (the Phase 2 shape cannot say "not observed") and adds
six tables. These tests prove four things that a schema diff cannot: the real database survives
untouched, the rebuild refuses to run over rows it cannot interpret, the anti-zero and value-kind
CHECK constraints actually fire in SQLite, and a downgrade restores the old shape exactly.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
from pathlib import Path

import pytest
from migration_helpers import alembic_head, ensure_revision, revision_before

PRODUCTION_DB = Path(os.environ.get("CREATOROS_ROOT") or Path.home() / "CreatorOS") / "data" / "database" / "creatoros.db"
HEAD = alembic_head()
PREVIOUS = revision_before(HEAD)

ACCOUNT = "01M90AAAAAAAAAAAAAAAAAAAAA"
RUN = "01M90BBBBBBBBBBBBBBBBBBBBB"
STAMP = "2026-01-01 00:00:00"

ANALYTICS_TABLES = {
    "analytics_metric_definition",
    "analytics_collection_run",
    "analytics_snapshot",
    "analytics_sync_state",
    "analytics_aggregation",
    "analytics_recommendation",
    "analytics_report",
}


def _alembic_config(root: Path):
    from alembic.config import Config

    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    return config


def _point_at(root: Path) -> None:
    import os

    from creatoros.core import settings as settings_mod
    from creatoros.db import session as session_mod

    os.environ["CREATOROS_ROOT"] = str(root)
    settings_mod.get_settings.cache_clear()
    settings_mod.resolve_root.cache_clear()
    session_mod.reset_engine()


def _dangling_references(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
    dangling: list[tuple[str, str]] = []
    for name, sql in connection.execute("select name, sql from sqlite_master where type='table'"):
        for match in re.finditer(r'REFERENCES\s+"?([A-Za-z0-9_]+)"?', sql or ""):
            if match.group(1) not in tables:
                dangling.append((name, match.group(1)))
    return dangling


@pytest.fixture()
def copied_production_db(tmp_path):
    if not PRODUCTION_DB.exists():
        pytest.skip("no production database on this machine")
    root = tmp_path / "prod-copy-root"
    target = root / "data" / "database"
    target.mkdir(parents=True)
    # The WAL holds the most recent commits; copying only the .db file silently loses them.
    for suffix in ("", "-wal", "-shm"):
        sidecar = Path(str(PRODUCTION_DB) + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, target / f"creatoros.db{suffix}")
    previous_root = os.environ.get("CREATOROS_ROOT")
    _point_at(root)
    yield root
    # A leaked root would make every later test read a database the fixture is about to delete.
    if previous_root is not None:
        os.environ["CREATOROS_ROOT"] = previous_root
        _point_at(Path(previous_root))


def _seed_account(connection: sqlite3.Connection) -> None:
    connection.execute("pragma foreign_keys=on")
    connection.execute(
        """
        insert into social_account (platform, provider_account_id, display_name, account_type,
            scopes_json, capabilities_json, connection_status, token_state, id, created_at, updated_at)
        values ('facebook', 'page-1', 'Page One', 'facebook_page', '[]', '{}', 'connected', 'valid', ?, ?, ?)
        """,
        (ACCOUNT, STAMP, STAMP),
    )


def _seed_metric(connection: sqlite3.Connection, key: str = "tt.video.views", value_kind: str = "scalar") -> None:
    # The production copy already carries the seeded registry (P8.4), so the fixture replaces the
    # one row it needs instead of assuming an empty table.
    connection.execute("delete from analytics_metric_definition where metric_key = ?", (key,))
    connection.execute(
        """
        insert into analytics_metric_definition (metric_key, platform, object_type, provider_metric_name,
            provider_object, source_endpoint, source_doc_url, unit, value_kind, metric_class,
            period_kinds_json, definition_text, is_estimate, is_cumulative, is_deprecated,
            required_permissions_json, account_threshold_json, availability_default, catalog_version,
            created_at, updated_at)
        values (?, 'tiktok', 'video', 'view_count', 'tiktok_video', '/v2/video/list/', 'https://example/',
            'count', ?, 'cumulative', '["lifetime"]', 'Number of views', 0, 1, 0, '["video.list"]', '{}',
            'not_collected', 'test', ?, ?)
        """,
        (key, value_kind, STAMP, STAMP),
    )


def _seed_run(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        insert into analytics_collection_run (account_id, platform, target_kind, started_at, outcome,
            http_calls, metrics_attempted, metrics_observed, metrics_unavailable, metrics_written,
            dedupe_key, id, created_at, updated_at)
        values (?, 'facebook', 'metrics', ?, 'succeeded', 1, 1, 1, 0, 1, 'dedupe-1', ?, ?, ?)
        """,
        (ACCOUNT, STAMP, RUN, STAMP, STAMP),
    )


def test_upgrade_keeps_the_real_database_intact(copied_production_db):
    from alembic import command

    config = _alembic_config(copied_production_db)
    command.upgrade(config, "head")
    db = copied_production_db / "data" / "database" / "creatoros.db"

    with sqlite3.connect(db) as connection:
        assert connection.execute("pragma integrity_check").fetchone()[0] == "ok"
        assert connection.execute("pragma foreign_key_check").fetchall() == []
        assert connection.execute("select version_num from alembic_version").fetchone()[0] == HEAD
        assert _dangling_references(connection) == []

        tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
        assert ANALYTICS_TABLES <= tables
        assert len(tables) == 48, f"expected 42 domain tables + 6 new, found {len(tables)}"

        columns = {row[1] for row in connection.execute("pragma table_info(analytics_snapshot)")}
        assert {
            "account_id",
            "platform",
            "object_type",
            "provider_object_id",
            "publication_id",
            "content_project_id",
            "campaign_id",
            "asset_id",
            "metric_key",
            "value_kind",
            "period_kind",
            "period_start",
            "period_end",
            "value_num",
            "value_json",
            "unit",
            "availability",
            "availability_reason",
            "breakdown_key",
            "api_version",
            "api_host",
            "source_ref",
            "run_id",
            "collected_at",
            "data_as_of",
            "completeness",
            "sample_size",
        } <= columns
        assert "value" not in columns and "hour_bucket" not in columns

        # Every Phase 1-7 fact survives the rebuild of an unrelated table.
        assert connection.execute("select count(*) from agent_definition").fetchone()[0] == 111
        assert connection.execute("select count(*) from agent_department").fetchone()[0] == 10
        assert connection.execute("select count(*) from media_asset").fetchone()[0] == (
            sqlite3.connect(f"file:{PRODUCTION_DB}?mode=ro", uri=True)
            .execute("select count(*) from media_asset")
            .fetchone()[0]
        )


def test_migration_refuses_to_drop_rows_it_cannot_interpret(copied_production_db):
    from alembic import command

    config = _alembic_config(copied_production_db)
    ensure_revision(config, PREVIOUS)
    db = copied_production_db / "data" / "database" / "creatoros.db"
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            insert into analytics_snapshot (platform, account_id, post_id, metric, value, unit,
                captured_at, hour_bucket, source, completeness, id, created_at, updated_at)
            values ('facebook', null, 'post-1', 'impressions', 12, 'count', ?, '2026-01-01T00', 'legacy',
                'complete', '01M90LEGACYROW00000000000', ?, ?)
            """,
            (STAMP, STAMP, STAMP),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="analytics_snapshot is not empty"):
        command.upgrade(config, "head")

    with sqlite3.connect(db) as connection:
        assert connection.execute("select count(*) from analytics_snapshot").fetchone()[0] == 1
        assert connection.execute("select version_num from alembic_version").fetchone()[0] == PREVIOUS


def test_downgrade_restores_the_phase_two_shape(copied_production_db):
    from alembic import command

    config = _alembic_config(copied_production_db)
    command.upgrade(config, "head")
    command.downgrade(config, PREVIOUS)
    db = copied_production_db / "data" / "database" / "creatoros.db"

    with sqlite3.connect(db) as connection:
        assert connection.execute("pragma foreign_key_check").fetchall() == []
        tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
        assert not (ANALYTICS_TABLES - {"analytics_snapshot"}) & tables
        columns = {row[1] for row in connection.execute("pragma table_info(analytics_snapshot)")}
        assert columns == {
            "platform",
            "account_id",
            "post_id",
            "metric",
            "value",
            "unit",
            "captured_at",
            "hour_bucket",
            "source",
            "completeness",
            "sample_size",
            "id",
            "created_at",
            "updated_at",
        }
        # SQLite materialises a named UNIQUE constraint as an autoindex, so prove the rule instead
        # of its name: the old (platform, post_id, metric, hour_bucket) key must still be unique.
        connection.execute(
            """
            insert into analytics_snapshot (platform, account_id, post_id, metric, value, unit,
                captured_at, hour_bucket, source, completeness, id, created_at, updated_at)
            values ('facebook', null, 'post-1', 'impressions', 1, 'count', ?, '2026-01-01T00', 'legacy',
                'complete', '01M90DOWNGRADE00000000001', ?, ?)
            """,
            (STAMP, STAMP, STAMP),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                insert into analytics_snapshot (platform, account_id, post_id, metric, value, unit,
                    captured_at, hour_bucket, source, completeness, id, created_at, updated_at)
                values ('facebook', null, 'post-1', 'impressions', 2, 'count', ?, '2026-01-01T00', 'legacy',
                    'complete', '01M90DOWNGRADE00000000002', ?, ?)
                """,
                (STAMP, STAMP, STAMP),
            )
        connection.rollback()

    command.upgrade(config, "head")
    with sqlite3.connect(db) as connection:
        assert connection.execute("pragma foreign_key_check").fetchall() == []
        assert connection.execute("select version_num from alembic_version").fetchone()[0] == HEAD


def test_the_anti_zero_and_value_kind_checks_fire(copied_production_db):
    from alembic import command

    config = _alembic_config(copied_production_db)
    command.upgrade(config, "head")
    db = copied_production_db / "data" / "database" / "creatoros.db"

    with sqlite3.connect(db) as connection:
        connection.execute("pragma foreign_keys=on")
        _seed_account(connection)
        _seed_metric(connection, "tt.video.views", "scalar")
        _seed_metric(connection, "ig.media.retention", "series")
        _seed_run(connection)

        # 1. An unavailable metric can never carry a value.
        with pytest.raises(sqlite3.IntegrityError, match="ck_analytics_unobserved_has_no_value"):
            connection.execute(
                """
                insert into analytics_snapshot (account_id, platform, object_type, provider_object_id,
                    metric_key, value_kind, period_kind, value_num, unit, availability, breakdown_key,
                    api_version, api_host, source_ref, run_id, collected_at, completeness, id, created_at, updated_at)
                values (?, 'tiktok', 'video', 'v1', 'tt.video.views', 'scalar', 'lifetime', 0,
                    'count', 'not_supported', '', 'v2', 'open.tiktokapis.com', '/v2/video/list/', ?,
                    ?, 'complete', '01M90BAD0000000000000001', ?, ?)
                """,
                (ACCOUNT, RUN, STAMP, STAMP, STAMP),
            )

        # 2. An observed metric must carry a value.
        with pytest.raises(sqlite3.IntegrityError, match="ck_analytics_observed_has_value"):
            connection.execute(
                """
                insert into analytics_snapshot (account_id, platform, object_type, provider_object_id,
                    metric_key, value_kind, period_kind, unit, availability, breakdown_key,
                    api_version, api_host, source_ref, run_id, collected_at, completeness, id, created_at, updated_at)
                values (?, 'tiktok', 'video', 'v1', 'tt.video.views', 'scalar', 'lifetime', 'count',
                    'observed', '', 'v2', 'open.tiktokapis.com', '/v2/video/list/', ?, ?, 'complete',
                    '01M90BAD0000000000000002', ?, ?)
                """,
                (ACCOUNT, RUN, STAMP, STAMP, STAMP),
            )

        # 3. A scalar cannot smuggle a curve into value_json.
        with pytest.raises(sqlite3.IntegrityError, match="ck_analytics_value_kind_matches_column"):
            connection.execute(
                """
                insert into analytics_snapshot (account_id, platform, object_type, provider_object_id,
                    metric_key, value_kind, period_kind, value_num, value_json, unit, availability, breakdown_key,
                    api_version, api_host, source_ref, run_id, collected_at, completeness, id, created_at, updated_at)
                values (?, 'tiktok', 'video', 'v1', 'tt.video.views', 'scalar', 'lifetime', 5, '[1,2]',
                    'count', 'observed', '', 'v2', 'open.tiktokapis.com', '/v2/video/list/', ?, ?, 'complete',
                    '01M90BAD0000000000000003', ?, ?)
                """,
                (ACCOUNT, RUN, STAMP, STAMP, STAMP),
            )

        # 4. A curve cannot be flattened into value_num.
        with pytest.raises(sqlite3.IntegrityError, match="ck_analytics_value_kind_matches_column"):
            connection.execute(
                """
                insert into analytics_snapshot (account_id, platform, object_type, provider_object_id,
                    metric_key, value_kind, period_kind, value_num, unit, availability, breakdown_key,
                    api_version, api_host, source_ref, run_id, collected_at, completeness, id, created_at, updated_at)
                values (?, 'instagram', 'reel', 'm1', 'ig.media.retention', 'series', 'lifetime', 42,
                    'json', 'observed', '', 'v23.0', 'graph.instagram.com', '/m1/insights', ?, ?, 'complete',
                    '01M90BAD0000000000000004', ?, ?)
                """,
                (ACCOUNT, RUN, STAMP, STAMP, STAMP),
            )

        # 5. The honest shapes are accepted, including a series with no scalar value.
        connection.execute(
            """
            insert into analytics_snapshot (account_id, platform, object_type, provider_object_id,
                metric_key, value_kind, period_kind, value_num, unit, availability, breakdown_key,
                api_version, api_host, source_ref, run_id, collected_at, completeness, id, created_at, updated_at)
            values (?, 'tiktok', 'video', 'v1', 'tt.video.views', 'scalar', 'lifetime', 1200,
                'count', 'observed', '', 'v2', 'open.tiktokapis.com', '/v2/video/list/', ?, ?, 'complete',
                '01M90BAD0000000000000005', ?, ?)
            """,
            (ACCOUNT, RUN, STAMP, STAMP, STAMP),
        )
        connection.execute(
            """
            insert into analytics_snapshot (account_id, platform, object_type, provider_object_id,
                metric_key, value_kind, period_kind, value_json, unit, availability, breakdown_key,
                api_version, api_host, source_ref, run_id, collected_at, completeness, id, created_at, updated_at)
            values (?, 'instagram', 'reel', 'm1', 'ig.media.retention', 'series', 'lifetime',
                '{"points": [100, 80, 60], "cardinality": 3}', 'json', 'observed', '', 'v23.0',
                'graph.instagram.com', '/m1/insights', ?, ?, 'complete',
                '01M90BAD0000000000000006', ?, ?)
            """,
            (ACCOUNT, RUN, STAMP, STAMP, STAMP),
        )
        assert connection.execute("select count(*) from analytics_snapshot").fetchone()[0] == 2


def test_account_deletion_cascades_and_publication_deletion_only_nulls_the_link(copied_production_db):
    from alembic import command

    config = _alembic_config(copied_production_db)
    command.upgrade(config, "head")
    db = copied_production_db / "data" / "database" / "creatoros.db"

    with sqlite3.connect(db) as connection:
        connection.execute("pragma foreign_keys=on")
        _seed_account(connection)
        _seed_metric(connection)
        _seed_run(connection)
        project = "01M90PROJECT00000000000001"
        variant = "01M90VARIANT00000000000001"
        job = "01M90JOB000000000000000001"
        connection.execute(
            "insert into content_project (title, status, id, created_at, updated_at) values ('P','draft',?,?,?)",
            (project, STAMP, STAMP),
        )
        connection.execute(
            """
            insert into platform_variant (project_id, platform, target_account_id, aspect_ratio, caption,
                hashtags_json, privacy_setting, validation_report_json, approval_status, id, created_at, updated_at)
            values (?, 'facebook', ?, '9:16', '', '[]', 'public', '{}', 'draft', ?, ?, ?)
            """,
            (project, ACCOUNT, variant, STAMP, STAMP),
        )
        connection.execute(
            """
            insert into publish_job (variant_id, platform, account_id, display_timezone, job_state, attempts,
                idempotency_key, remote_status_json, remote_post_id, id, created_at, updated_at)
            values (?, 'facebook', ?, 'UTC', 'published', 1, 'k1', '{}', 'remote-1', ?, ?, ?)
            """,
            (variant, ACCOUNT, job, STAMP, STAMP),
        )
        connection.execute(
            """
            insert into analytics_snapshot (account_id, platform, object_type, provider_object_id,
                publication_id, content_project_id, campaign_id, metric_key, value_kind, period_kind,
                value_num, unit, availability, breakdown_key, api_version, api_host, source_ref, run_id,
                collected_at, completeness, id, created_at, updated_at)
            values (?, 'tiktok', 'video', 'v1', ?, ?, '', 'tt.video.views', 'scalar', 'lifetime', 10,
                'count', 'observed', '', 'v2', 'open.tiktokapis.com', '/v2/video/list/', ?, ?, 'complete',
                '01M90SNAP00000000000000001', ?, ?)
            """,
            (ACCOUNT, job, project, RUN, STAMP, STAMP, STAMP),
        )

        # Deleting the publication keeps the measurement; only the link is cleared.
        connection.execute("delete from publish_job where id = ?", (job,))
        row = connection.execute(
            "select publication_id, content_project_id, value_num from analytics_snapshot"
        ).fetchone()
        assert row == (None, project, 10.0)

        # Deleting the account removes its analytics, because a revoked account must not leave data.
        connection.execute("delete from social_account where id = ?", (ACCOUNT,))
        assert connection.execute("select count(*) from analytics_snapshot").fetchone()[0] == 0
        assert connection.execute("select count(*) from analytics_collection_run").fetchone()[0] == 0
