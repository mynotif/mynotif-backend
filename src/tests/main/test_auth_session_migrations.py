import pytest
from django.db import connection
from django.db.migrations.loader import MigrationLoader

pytestmark = pytest.mark.django_db


def test_auth_session_migration_depends_on_rls_and_nurse_schema():
    loader = MigrationLoader(connection)
    dependencies = loader.graph.node_map[("main", "0002_row_level_security")].children
    assert any(node.key == ("main", "0003_auth_sessions") for node in dependencies)
    assert ("nurse", "0001_initial") in loader.disk_migrations[
        ("main", "0002_row_level_security")
    ].dependencies


def test_auth_session_tables_exist_after_migrations():
    tables = set(connection.introspection.table_names())
    assert "main_authsession" in tables
    assert "main_authtoken" in tables
