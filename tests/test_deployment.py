import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[1]


def production_compose_config() -> dict:
    environment = os.environ.copy()
    environment.update({
        "APP_DOMAIN": "dfo.example.test",
        "CADDY_SITE_ADDRESS": "http://dfo.example.test",
        "CADDY_HEALTH_URL": "http://localhost",
        "COMPOSE_PROJECT_NAME": "dfo-spm-test",
    })
    result = subprocess.run(
        ["docker", "compose", "--env-file", ".env.production.example", "-f", "compose.production.yml", "config", "--format", "json"],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_production_compose_is_hardened_and_persistent():
    config = production_compose_config()
    services = config["services"]
    assert set(services) >= {"app", "postgres", "migrate", "bootstrap", "caddy"}
    assert "ports" not in services["postgres"]
    assert services["app"]["read_only"] is True
    assert "no-new-privileges:true" in services["app"]["security_opt"]
    assert services["app"]["cap_drop"] == ["ALL"]
    assert set(services["app"]["cap_add"]) == {"CHOWN", "DAC_OVERRIDE", "KILL", "SETGID", "SETUID"}
    assert services["app"]["healthcheck"]["test"][-1] == "http://localhost:8080/ready"
    assert services["postgres"]["healthcheck"]
    assert services["caddy"]["healthcheck"]["test"][0] == "CMD-SHELL"
    assert any(mount["target"] == "/app/data/attachments" for mount in services["app"]["volumes"])
    assert {published for port in services["caddy"]["ports"] for published in [port["published"]]} >= {"80", "443"}
    assert set(config["secrets"]) >= {"db_password", "admin_password"}
    assert services["app"]["depends_on"]["bootstrap"]["condition"] == "service_completed_successfully"
    assert services["bootstrap"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert "admin_password" not in services["app"].get("secrets", [])
    assert "SPM_DEFAULT_ADMIN_PASSWORD_FILE" not in services["app"]["environment"]


def test_caddy_supports_an_existing_external_tls_proxy():
    config = production_compose_config()
    caddy = config["services"]["caddy"]
    assert caddy["environment"]["APP_DOMAIN"] == "dfo.example.test"
    assert caddy["environment"]["CADDY_SITE_ADDRESS"] == "http://dfo.example.test"
    assert caddy["environment"]["CADDY_HEALTH_URL"] == "http://localhost"
    assert caddy["healthcheck"]["test"] == [
        "CMD-SHELL",
        'curl -fkSs --resolve "$${APP_DOMAIN}:443:127.0.0.1" -H "Host: $${APP_DOMAIN}" "$${CADDY_HEALTH_URL}/health"',
    ]
    assert "{$CADDY_SITE_ADDRESS}" in (ROOT / "deploy" / "Caddyfile").read_text()


def test_example_environment_follows_app_domain_for_direct_tls():
    environment = os.environ.copy()
    environment["APP_DOMAIN"] = "spm.example.test"
    result = subprocess.run(
        ["docker", "compose", "--env-file", ".env.production.example", "-f", "compose.production.yml", "config", "--format", "json"],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    caddy = json.loads(result.stdout)["services"]["caddy"]
    assert caddy["environment"]["CADDY_SITE_ADDRESS"] == "spm.example.test"
    assert caddy["environment"]["CADDY_HEALTH_URL"] == "https://spm.example.test"


def test_container_image_runs_as_non_root_with_runtime_only_dependencies():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "FROM python:3.12-slim-bookworm" in dockerfile
    assert "pip install --no-cache-dir --require-hashes" in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]' in dockerfile
    assert 'CMD ["uvicorn"' in dockerfile
    requirements = (ROOT / "requirements-prod.txt").read_text()
    assert "pytest" not in requirements
    assert "httpx" not in requirements


def test_container_entrypoint_stages_secrets_before_dropping_privileges():
    entrypoint = (ROOT / "deploy" / "docker-entrypoint.sh").read_text()
    assert "/tmp/app-secrets" in entrypoint
    assert "rm -rf /tmp/app-secrets" in entrypoint
    assert "install -m 0400" in entrypoint
    assert "chown -R 10001:10001 /tmp/app-secrets" in entrypoint
    assert "setpriv --reuid=10001 --regid=10001 --init-groups" in entrypoint


def test_docker_build_context_excludes_secrets_and_runtime_data():
    ignored = (ROOT / ".dockerignore").read_text().splitlines()
    assert ".env" in ignored
    assert ".env.*" in ignored
    assert "!.env.production.example" in ignored
    assert ".git" in ignored
    assert "data/attachments" in ignored


def test_caddy_configuration_is_readable_by_the_hardened_proxy():
    mode = (ROOT / "deploy" / "Caddyfile").stat().st_mode
    assert mode & 0o444 == 0o444


def test_backup_and_restore_scripts_have_safety_controls():
    backup = (ROOT / "deploy" / "backup.sh").read_text()
    restore = (ROOT / "deploy" / "restore.sh").read_text()
    assert "umask 077" in backup
    assert "pg_dump --format=custom" in backup
    assert "sha256sum" in backup
    assert '"${COMPOSE[@]}" stop caddy app' in backup
    assert '"${COMPOSE[@]}" up -d --wait app caddy' in backup
    assert "flock -n" in backup
    assert "RESTORE_CONFIRM=restore" in restore
    assert "sha256sum --check" in restore
    assert "flock -n" in restore
    assert "pg_restore --list" in restore
    assert "staging_db" in restore
    assert "rollback" in restore
    assert "schema_revision=" in backup
    assert "MANIFEST" in restore
    assert "compose_project" in restore
    assert "EXPECTED_SCHEMA_REVISION" in restore
    assert "Restored schema revision does not match" in restore
    assert "rollback_failed=false" in restore
    assert "Manual recovery is required; application traffic remains stopped" in restore
    assert "python -m app.verify_attachments" in backup
    assert "Backup failed and application restart also failed" in backup
    assert "python -m app.verify_attachments" in restore
    assert 'SPM_DB_NAME="${staging_db}"' in restore
    assert 'SPM_ATTACHMENT_DIR="/app/data/attachments/${staging_dir}"' in restore
    assert 'ALTER DATABASE \\"$live\\" RENAME TO \\"$failed\\"' in restore
    assert 'dropdb --force --if-exists --username "$POSTGRES_USER" "$live"' not in restore
    assert '"${COMPOSE[@]}" stop caddy app >/dev/null 2>&1 || true' in restore
    for script in (ROOT / "deploy" / "backup.sh", ROOT / "deploy" / "restore.sh"):
        subprocess.run(["bash", "-n", script], check=True)


def test_migration_does_not_automatically_stamp_legacy_schemas():
    migration_runner = (ROOT / "app" / "migrate.py").read_text()
    assert "command.stamp" not in migration_runner
    assert "operator-approved baseline" in migration_runner
