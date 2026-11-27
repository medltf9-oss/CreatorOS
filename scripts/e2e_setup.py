#!/usr/bin/env python3
"""Process-level end-to-end harness for the CreatorOS first-run setup wizard.

This harness starts the *real* backend application (the same "python -m creatoros serve"
entry point the Electron shell spawns), against a throwaway CREATOROS_ROOT, and drives the
whole setup journey over real HTTP: status, verify (failure and success), status reflecting
the recorded verification, a 409 refusal, an explicit override, persistence across a process
restart, and the "version_changed" upgrade signal.

There is no FastAPI TestClient, no monkeypatching, no dependency override and no fake setup
service here. The pins come from the manifest that ships inside the package
(app/backend/src/creatoros/resources/prerequisites.v1.json), and the verified bytes are the
repository's real FFmpeg build (tools/ffmpeg/bin/ffmpeg.exe), whose SHA-256 is checked against
that manifest before the journey starts. The failure case hashes a deliberately tampered file.

Run it with the backend virtualenv (stdlib only, so any Python 3.11+ works):

    app/backend/.venv/Scripts/python.exe scripts/e2e_setup.py

Options:

    --with-ui   also run the Electron desktop E2E (app/desktop/tests/setupE2E.test.mjs)
    --keep      keep the temporary data roots for inspection
    --python    interpreter used for the backend processes
    --repo      repository root (defaults to this file's parent)

Exit codes: 0 all checks passed, 1 a check failed, 2 the harness itself could not run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_RELATIVE = Path("app/backend/src/creatoros/resources/prerequisites.v1.json")
FFMPEG_BIN_RELATIVE = Path("tools/ffmpeg/bin")
UPGRADE_VERSION = "9.9.9"


class E2EFailure(RuntimeError):
    """A user-visible assertion failed; the journey stops at the failing step."""


class HarnessError(RuntimeError):
    """The environment cannot run the harness (missing interpreter, payload, manifest...)."""


@dataclass
class Step:
    name: str
    ok: bool
    detail: str


@dataclass
class Report:
    """Collects named checks and counts real HTTP calls."""

    steps: list[Step] = field(default_factory=list)
    http_calls: int = 0
    started: float = field(default_factory=time.monotonic)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        step = Step(name=name, ok=bool(ok), detail=detail)
        self.steps.append(step)
        marker = "PASS" if ok else "FAIL"
        print(f"[{marker}] {len(self.steps):02d} {name}" + (f" :: {detail}" if detail else ""), flush=True)
        if not ok:
            raise E2EFailure(f"{name}: {detail or 'check failed'}")

    def expect(self, name: str, actual: Any, expected: Any) -> None:
        self.check(
            name,
            actual == expected,
            f"expected {expected!r}, got {actual!r}" if actual != expected else f"got {expected!r}",
        )

    def contains(self, name: str, text: Any, needle: str) -> None:
        haystack = str(text or "")
        self.check(name, needle in haystack, f"expected {needle!r} in {haystack!r}" if needle not in haystack else f"found {needle!r}")

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(step.ok for step in self.steps)

    def summary(self, journeys: list[str]) -> str:
        elapsed = time.monotonic() - self.started
        lines = [
            "",
            "=" * 72,
            f"checks passed : {sum(1 for s in self.steps if s.ok)}/{len(self.steps)}",
            f"http requests : {self.http_calls}",
            f"journeys      : {len(journeys)}",
            f"elapsed       : {elapsed:.2f}s",
        ]
        lines.extend(f"  - {name}" for name in journeys)
        lines.append("=" * 72)
        return "\n".join(lines)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def repo_manifest(repo: Path) -> dict[str, Any]:
    path = repo / MANIFEST_RELATIVE
    if not path.is_file():
        raise HarnessError(f"pinned manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def pinned_component(manifest: dict[str, Any], component_id: str) -> dict[str, Any]:
    for component in manifest.get("components", []):
        if component.get("id") == component_id:
            return component
    raise HarnessError(f"component {component_id!r} is not pinned in the manifest")


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


@dataclass
class HttpResult:
    status: int
    body: Any
    headers: dict[str, str]

    def json(self) -> Any:
        return self.body if self.body is not None else {}


class BackendService:
    """The real FastAPI app under uvicorn, one process, one temporary data root."""

    def __init__(
        self,
        *,
        repo: Path,
        python: Path,
        root: Path,
        token: str,
        report: Report,
        version: str | None = None,
        label: str = "serve",
    ) -> None:
        self.backend_dir = repo / "app/backend"
        self.python = python
        self.root = root
        self.token = token
        self.report = report
        self.version = version
        self.label = label
        self.port = 0
        self.proc: subprocess.Popen[bytes] | None = None
        logs = root / "e2e-logs"
        logs.mkdir(parents=True, exist_ok=True)
        self.log_path = logs / f"{label}.log"
        self._log_handle: Any = None

    def env(self) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("CREATOROS_")}
        env.update(
            {
                "CREATOROS_ROOT": str(self.root),
                "CREATOROS_SESSION_TOKEN": self.token,
                # Test isolation only: no background worker polls and no fixed OAuth callback
                # port is bound. Neither flag touches the setup routes.
                "CREATOROS_DISABLE_WORKERS": "1",
                "CREATOROS_SOCIAL_CALLBACK_ENABLED": "0",
                "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        if self.version:
            env["CREATOROS_VERSION"] = self.version
        return env

    def migrate(self) -> None:
        result = subprocess.run(
            [str(self.python), "-m", "creatoros", "migrate"],
            cwd=self.backend_dir,
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if result.returncode != 0:
            raise HarnessError(
                "creatoros migrate failed in "
                + f"{self.root}\nrc={result.returncode}\nstdout tail:\n{result.stdout[-2000:]}\nstderr tail:\n{result.stderr[-2000:]}"
            )

    def start(self) -> "BackendService":
        self.migrate()
        self.port = _free_port()
        self._log_handle = self.log_path.open("ab")
        self.proc = subprocess.Popen(
            [
                str(self.python),
                "-m",
                "creatoros",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ],
            cwd=self.backend_dir,
            env=self.env(),
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
        )
        self._wait_until_live()
        return self

    def _wait_until_live(self, timeout: float = 180.0) -> None:
        deadline = time.monotonic() + timeout
        last_error = "no attempt"
        url = f"http://127.0.0.1:{self.port}/health/live"
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise HarnessError(
                    f"backend exited before /health/live (rc={self.proc.returncode}); log: {self.log_path}\n{self.log_tail()}"
                )
            try:
                with urllib.request.urlopen(url, timeout=3.0) as response:
                    if response.status == 200:
                        return
                    last_error = f"HTTP {response.status}"
            except Exception as exc:  # noqa: BLE001 - a starting server refuses or times out
                last_error = repr(exc)
            time.sleep(0.25)
        raise HarnessError(f"backend did not become live within {timeout:.0f}s ({last_error}); log: {self.log_path}\n{self.log_tail()}")

    def request(
        self,
        method: str,
        path: str,
        payload: Any = None,
        *,
        authenticated: bool = True,
        timeout: float = 120.0,
    ) -> HttpResult:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers: dict[str, str] = {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if authenticated:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        self.report.http_calls += 1
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return HttpResult(response.status, json.loads(raw) if raw else None, dict(response.headers))
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                body = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                body = {"detail": raw.decode("utf-8", "replace")}
            return HttpResult(exc.code, body, dict(exc.headers))
        except (urllib.error.URLError, OSError) as exc:
            raise HarnessError(f"{method} {path} could not reach the backend: {exc!r}; log: {self.log_tail()}") from exc

    def get(self, path: str, **kwargs: Any) -> HttpResult:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, payload: Any = None, **kwargs: Any) -> HttpResult:
        return self.request("POST", path, payload, **kwargs)

    def log_tail(self, lines: int = 40) -> str:
        try:
            content = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return "<no log>"
        return "\n".join(content[-lines:])

    def stop(self) -> None:
        # FastAPI runs a yield dependency's commit after the response is handed back; one more
        # round trip makes sure the last write's teardown ran before this process is terminated.
        if self.proc is not None and self.proc.poll() is None:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health/live", timeout=3.0).close()
                time.sleep(0.25)
            except Exception:  # noqa: BLE001 - shutdown must never fail on a dead server
                pass
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        self.proc = None

    def __enter__(self) -> "BackendService":
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()


def _setting(root: Path, key: str) -> dict[str, Any] | None:
    database = root / "data/database/creatoros.db"
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        row = connection.execute("select value_json from app_setting where key = ?", (key,)).fetchone()
    finally:
        connection.close()
    if row is None or row[0] is None:
        return None
    value = row[0]
    return json.loads(value) if isinstance(value, str) else dict(value)


def _audit(root: Path, action: str) -> list[dict[str, Any]]:
    database = root / "data/database/creatoros.db"
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "select action, outcome, metadata_redacted_json, resource_id from audit_event"
            " where action = ? order by created_at asc",
            (action,),
        ).fetchall()
    finally:
        connection.close()
    events = []
    for row in rows:
        metadata = row["metadata_redacted_json"]
        events.append(
            {
                "action": row["action"],
                "outcome": row["outcome"],
                "resource_id": row["resource_id"],
                "metadata": json.loads(metadata) if isinstance(metadata, str) else dict(metadata or {}),
            }
        )
    return events


def _check_map(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {check["id"]: check for check in status.get("checks", [])}


def _component(status: dict[str, Any], component_id: str) -> dict[str, Any]:
    for component in status.get("prerequisites", []):
        if component.get("id") == component_id:
            return component
    raise E2EFailure(f"prerequisite {component_id!r} missing from the status payload")


def _prepare_root(label: str, report: Report) -> Path:
    root = Path(tempfile.mkdtemp(prefix=f"creatoros-e2e-{label}-")).resolve()
    report.check(f"{label}: temporary CREATOROS_ROOT created", root.is_dir(), str(root))
    return root


def _finish_root(root: Path, *, keep: bool) -> None:
    if keep:
        print(f"[keep] data root retained: {root}", flush=True)
        return
    shutil.rmtree(root, ignore_errors=True)


def run_override_journey(repo: Path, python: Path, report: Report, *, keep: bool = False) -> dict[str, Any]:
    """409 -> explicit override -> restart -> upgrade, against a root without FFmpeg in place."""
    label = "override"
    root = _prepare_root(label, report)
    manifest_path = repo / MANIFEST_RELATIVE
    manifest_sha = sha256_file(manifest_path)
    try:
        manifest = repo_manifest(repo)
        ffmpeg = pinned_component(manifest, "ffmpeg")
        acquisition = ffmpeg["acquisition"]
        pinned_sha = str(acquisition["sha256"]).lower()
        pinned_size = int(acquisition["size_bytes"])
        payload = repo / FFMPEG_BIN_RELATIVE / "ffmpeg.exe"
        if not payload.is_file():
            raise HarnessError(f"the real FFmpeg payload pinned by the manifest is missing: {payload}")

        # Prove the manifest pin and the bytes used in this journey are the same real artifact.
        report.expect(f"{label}: shipped manifest pins the repository FFmpeg build", sha256_file(payload), pinned_sha)
        report.expect(f"{label}: pinned size matches the repository FFmpeg build", payload.stat().st_size, pinned_size)
        downloads = root / "temp/downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        verified_copy = downloads / "ffmpeg.exe"
        shutil.copy2(payload, verified_copy)
        tampered = downloads / "tampered-ffmpeg.exe"
        tampered.write_bytes(b"this is not the pinned FFmpeg build\n" * 4096)

        token = f"e2e-{secrets.token_hex(8)}"
        service = BackendService(repo=repo, python=python, root=root, token=token, report=report, label="override-1")
        with service:
            status = service.get("/setup/status").json()
            base_version = str(status.get("version") or "")
            report.check(f"{label}: /setup/status reports a backend version", bool(base_version), base_version)
            report.expect(f"{label}: fresh root is not complete", status.get("setup_complete"), False)
            report.expect(f"{label}: fresh root has no recorded first_run", status.get("first_run", {}).get("completed"), False)
            report.expect(f"{label}: manifest loaded from the package", status.get("manifest_error"), None)
            report.expect(f"{label}: status reports the manifest update date", status.get("manifest_updated"), manifest.get("updated"))
            ffmpeg_state = _component(status, "ffmpeg")
            report.expect(f"{label}: FFmpeg is absent from the app path", ffmpeg_state.get("state"), "missing")
            report.expect(f"{label}: presence alone never claims verification", ffmpeg_state.get("verified"), False)
            report.expect(f"{label}: no verification record exists yet", ffmpeg_state.get("verification"), None)
            checks = _check_map(status)
            report.expect(f"{label}: media.ffmpeg fails without the tool", checks["media.ffmpeg"]["state"], "fail")
            report.expect(f"{label}: data.root passes", checks["data.root"]["state"], "ok")
            report.expect(f"{label}: the pinned manifest passes", checks["prerequisites.manifest"]["state"], "ok")

            failed = service.post("/setup/prerequisites/ffmpeg/verify", {"path": str(tampered)})
            report.expect(f"{label}: verify of a tampered file answers 200", failed.status, 200)
            failed_body = failed.json()
            report.expect(f"{label}: tampered file is not verified", failed_body.get("ok"), False)
            report.expect(f"{label}: tampered hash is recorded as observed", failed_body.get("sha256"), sha256_file(tampered))
            report.expect(f"{label}: tampered verification record is not ok", failed_body.get("verification", {}).get("ok"), False)
            failed_checks = {check["id"]: check for check in failed_body.get("checks", [])}
            report.expect(f"{label}: the hash check fails", failed_checks["file.sha256"]["state"], "fail")

            after_failure = service.get("/setup/status").json()
            ffmpeg_after_failure = _component(after_failure, "ffmpeg")
            report.expect(f"{label}: status reports the failed verification", ffmpeg_after_failure.get("state"), "unverified")
            report.expect(f"{label}: failed record is kept, not discarded", ffmpeg_after_failure.get("verification", {}).get("ok"), False)
            report.contains(f"{label}: failed record is explained", ffmpeg_after_failure.get("detail"), "failed")

            rejected = service.post("/setup/prerequisites/ffmpeg/verify", {"path": "relative/ffmpeg.exe"})
            report.expect(f"{label}: a relative path is rejected", rejected.status, 422)
            report.expect(
                f"{label}: relative path rejection names the reason",
                rejected.json().get("code"),
                "PATH_NOT_ABSOLUTE",
            )

            passed = service.post("/setup/prerequisites/ffmpeg/verify", {"path": str(verified_copy)})
            report.expect(f"{label}: verify of the pinned payload answers 200", passed.status, 200)
            passed_body = passed.json()
            report.expect(f"{label}: pinned payload verifies", passed_body.get("ok"), True)
            report.expect(f"{label}: verified hash equals the manifest pin", passed_body.get("sha256"), pinned_sha)
            report.expect(f"{label}: verified size equals the manifest pin", passed_body.get("size_bytes"), pinned_size)
            report.expect(f"{label}: verification object is recorded", passed_body.get("verification", {}).get("ok"), True)
            report.expect(f"{label}: recorded verification is current", passed_body.get("verification", {}).get("current"), True)
            passed_checks = {check["id"]: check for check in passed_body.get("checks", [])}
            report.expect(f"{label}: the hash check passes", passed_checks["file.sha256"]["state"], "pass")
            verified_at = str(passed_body.get("verification", {}).get("verified_at") or "")
            report.check(f"{label}: verification carries a timestamp", bool(verified_at), verified_at)

            after_success = service.get("/setup/status").json()
            ffmpeg_after_success = _component(after_success, "ffmpeg")
            report.expect(f"{label}: status reflects the recorded verification", ffmpeg_after_success.get("state"), "verified")
            report.expect(f"{label}: status marks the component verified", ffmpeg_after_success.get("verified"), True)
            report.expect(
                f"{label}: status keeps the verified path",
                ffmpeg_after_success.get("verification", {}).get("path"),
                str(verified_copy),
            )
            report.expect(
                f"{label}: status recomputes currency from the file",
                ffmpeg_after_success.get("verification", {}).get("current"),
                True,
            )
            report.contains(f"{label}: status says the result is recorded, not live", ffmpeg_after_success.get("detail"), "not a live re-hash")
            catalog = service.get("/setup/prerequisites").json()
            catalog_ffmpeg = {item["id"]: item for item in catalog.get("components", [])}["ffmpeg"]
            report.expect(f"{label}: prerequisite catalog agrees", catalog_ffmpeg.get("verified"), True)

            refused = service.post("/setup/complete", {})
            report.expect(f"{label}: completion is refused while media.ffmpeg fails", refused.status, 409)
            refused_body = refused.json()
            report.expect(f"{label}: refusal carries the required-checks code", refused_body.get("code"), "REQUIRED_CHECKS_FAILING")
            report.contains(f"{label}: refusal names the failing check", refused_body.get("message"), "media.ffmpeg")
            # The envelope shape matters: the code lives in 'code', the ids in 'message'.
            report.expect(f"{label}: refusal message is the bare id list", refused_body.get("message"), "media.ffmpeg")
            still_open = service.get("/setup/status").json()
            report.expect(f"{label}: a refused completion records nothing", still_open.get("setup_complete"), False)

            override = service.post(
                "/setup/complete",
                {"override_required_checks": True, "notes": "e2e: FFmpeg deliberately not installed"},
            )
            report.expect(f"{label}: explicit override completes setup", override.status, 200)
            override_body = override.json()
            report.expect(f"{label}: override response reports completion", override_body.get("setup_complete"), True)
            report.expect(f"{label}: override is a first completion", override_body.get("already_completed"), False)
            first_run = override_body.get("first_run", {})
            report.expect(f"{label}: first_run is recorded", first_run.get("completed"), True)
            report.expect(f"{label}: recorded version is the running version", first_run.get("version"), base_version)
            report.expect(f"{label}: same version means version_changed is false", first_run.get("version_changed"), False)
            completed_at = str(first_run.get("completed_at") or "")
            report.check(f"{label}: completion carries a timestamp", bool(completed_at), completed_at)

            after_complete = service.get("/setup/status").json()
            report.expect(f"{label}: status reports setup complete", after_complete.get("setup_complete"), True)
            report.expect(
                f"{label}: status keeps the completion timestamp",
                after_complete.get("first_run", {}).get("completed_at"),
                completed_at,
            )
            report.expect(f"{label}: status still reports the failing check", _check_map(after_complete)["media.ffmpeg"]["state"], "fail")
            report.expect(
                f"{label}: status still reports the recorded verification",
                _component(after_complete, "ffmpeg").get("verified"),
                True,
            )
            report.check(
                f"{label}: no version change is reported on the same version",
                "setup.version_changed" not in _check_map(after_complete),
                str(sorted(_check_map(after_complete))),
            )

        state = _setting(root, "setup.first_run") or {}
        report.expect(f"{label}: completion is persisted in app_setting", state.get("completed"), True)
        report.expect(f"{label}: override flag is sticky in the record", state.get("override_required_checks"), True)
        report.expect(f"{label}: notes are persisted", state.get("notes"), "e2e: FFmpeg deliberately not installed")
        report.expect(f"{label}: completed version is persisted", state.get("completed_version"), base_version)
        report.expect(f"{label}: completion timestamp is stable in the record", state.get("completed_at"), completed_at)

        records = (_setting(root, "setup.verification") or {}).get("verifications", {})
        report.expect(f"{label}: verification record is persisted", records.get("ffmpeg", {}).get("ok"), True)
        report.expect(f"{label}: persisted record points at the verified copy", records.get("ffmpeg", {}).get("path"), str(verified_copy))
        report.expect(f"{label}: persisted record stores the pinned hash", records.get("ffmpeg", {}).get("sha256"), pinned_sha)

        verify_events = _audit(root, "setup.prerequisite.verify")
        report.expect(f"{label}: audit has one event per verify call", len(verify_events), 2)
        report.expect(f"{label}: audit records the failed attempt", verify_events[0].get("outcome"), "failure")
        report.expect(f"{label}: audit records the successful attempt", verify_events[1].get("outcome"), "success")
        report.expect(
            f"{label}: audit metadata carries the verified hash",
            verify_events[1].get("metadata", {}).get("sha256"),
            pinned_sha,
        )
        complete_events = _audit(root, "setup.first_run.complete")
        report.expect(f"{label}: completion is audited", len(complete_events), 1)
        complete_metadata = complete_events[0].get("metadata", {})
        report.expect(f"{label}: audit records the override decision", complete_metadata.get("override_required_checks"), True)
        report.expect(f"{label}: audit names the failing check", complete_metadata.get("failed_checks"), ["media.ffmpeg"])
        report.expect(f"{label}: audit records the completed version", complete_metadata.get("version"), base_version)

        # --- process restart: state must survive it ---------------------------------
        with BackendService(
            repo=repo, python=python, root=root, token=token, report=report, label="override-restart"
        ) as restarted:
            status = restarted.get("/setup/status").json()
            report.expect(f"{label}: restart keeps setup complete", status.get("setup_complete"), True)
            report.expect(
                f"{label}: restart keeps the completion timestamp",
                status.get("first_run", {}).get("completed_at"),
                completed_at,
            )
            report.expect(
                f"{label}: restart keeps the recorded verification",
                _component(status, "ffmpeg").get("verified"),
                True,
            )
            report.expect(
                f"{label}: restart recomputes currency from the real file",
                _component(status, "ffmpeg").get("verification", {}).get("current"),
                True,
            )
            report.expect(f"{label}: restart reports no version change", status.get("first_run", {}).get("version_changed"), False)

        # --- upgrade: a new running version must not reopen the wizard --------------
        with BackendService(
            repo=repo,
            python=python,
            root=root,
            token=token,
            report=report,
            version=UPGRADE_VERSION,
            label="override-upgrade",
        ) as upgraded:
            status = upgraded.get("/setup/status").json()
            report.expect(f"{label}: upgraded process reports the new version", status.get("version"), UPGRADE_VERSION)
            report.expect(f"{label}: upgrade does not reopen the wizard", status.get("setup_complete"), True)
            report.expect(
                f"{label}: upgrade keeps the completed version",
                status.get("first_run", {}).get("version"),
                base_version,
            )
            report.expect(f"{label}: upgrade flags version_changed", status.get("first_run", {}).get("version_changed"), True)
            version_check = _check_map(status)["setup.version_changed"]
            report.expect(f"{label}: upgrade raises a warning check", version_check["state"], "warn")
            report.contains(f"{label}: warning says the wizard does not reopen", version_check["detail"], "does not reopen")
            again = upgraded.post("/setup/complete", {"override_required_checks": True})
            report.expect(f"{label}: re-completion after upgrade succeeds", again.status, 200)
            again_body = again.json()
            report.expect(f"{label}: re-completion is idempotent", again_body.get("already_completed"), True)
            report.expect(
                f"{label}: re-completion never rewrites the completed version",
                again_body.get("first_run", {}).get("version"),
                base_version,
            )
            report.expect(
                f"{label}: re-completion keeps the original timestamp",
                again_body.get("first_run", {}).get("completed_at"),
                completed_at,
            )
            settled = upgraded.get("/setup/status").json()
            report.expect(f"{label}: upgraded process still reports completion", settled.get("setup_complete"), True)

        final_state = _setting(root, "setup.first_run") or {}
        report.expect(f"{label}: upgrade kept the original completion timestamp", final_state.get("completed_at"), completed_at)
        report.expect(f"{label}: upgrade kept the original completed version", final_state.get("completed_version"), base_version)
        report.expect(f"{label}: completion audits accumulate", len(_audit(root, "setup.first_run.complete")), 2)
        report.expect(f"{label}: the pinned manifest is byte-for-byte unchanged", sha256_file(manifest_path), manifest_sha)
        return {"root": str(root), "completed_at": completed_at, "base_version": base_version}
    finally:
        _finish_root(root, keep=keep)


def run_happy_journey(repo: Path, python: Path, report: Report, *, keep: bool = False) -> dict[str, Any]:
    """Present-but-unverified -> verify -> completion without an override."""
    label = "happy"
    root = _prepare_root(label, report)
    try:
        manifest = repo_manifest(repo)
        ffmpeg_pin = pinned_component(manifest, "ffmpeg")["acquisition"]
        pinned_sha = str(ffmpeg_pin["sha256"]).lower()
        pinned_size = int(ffmpeg_pin["size_bytes"])
        source = repo / FFMPEG_BIN_RELATIVE
        installed = root / "tools/ffmpeg/bin"
        installed.mkdir(parents=True, exist_ok=True)
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            if not (source / name).is_file():
                raise HarnessError(f"the real FFmpeg build is missing: {source / name}")
            shutil.copy2(source / name, installed / name)

        token = f"e2e-{secrets.token_hex(8)}"
        with BackendService(repo=repo, python=python, root=root, token=token, report=report, label="happy-1") as service:
            status = service.get("/setup/status").json()
            base_version = str(status.get("version") or "")
            report.expect(f"{label}: fresh root is not complete", status.get("setup_complete"), False)
            ffmpeg_state = _component(status, "ffmpeg")
            report.expect(f"{label}: installed FFmpeg is present", ffmpeg_state.get("state"), "ok")
            report.expect(f"{label}: presence is not verification", ffmpeg_state.get("verified"), False)
            report.expect(f"{label}: no verification record exists yet", ffmpeg_state.get("verification"), None)
            report.contains(f"{label}: detail says bytes are unverified", ffmpeg_state.get("detail"), "integrity is unverified")
            report.contains(f"{label}: detail says the binary is not executed", ffmpeg_state.get("detail"), "not run until /verify succeeds")
            checks = _check_map(status)
            report.expect(f"{label}: media.ffmpeg passes", checks["media.ffmpeg"]["state"], "ok")
            report.expect(f"{label}: no required check is failing", sorted(cid for cid, c in checks.items() if c["state"] == "fail"), [])

            verified = service.post("/setup/prerequisites/ffmpeg/verify", {})
            report.expect(f"{label}: verify discovers the tool in the app path", verified.status, 200)
            verified_body = verified.json()
            report.expect(f"{label}: discovered payload verifies", verified_body.get("ok"), True)
            report.expect(f"{label}: discovered payload matches the manifest", verified_body.get("sha256"), pinned_sha)
            report.expect(f"{label}: discovered payload size matches the manifest", verified_body.get("size_bytes"), pinned_size)
            report.expect(f"{label}: discovery resolved the app-path copy", verified_body.get("path"), str(installed / "ffmpeg.exe"))

            after = service.get("/setup/status").json()
            report.expect(f"{label}: status now reports the component verified", _component(after, "ffmpeg").get("verified"), True)

            completed = service.post("/setup/complete", {"skipped_ai": False})
            report.expect(f"{label}: passing checks complete without an override", completed.status, 200)
            completed_body = completed.json()
            report.expect(f"{label}: completion is reported", completed_body.get("setup_complete"), True)
            report.expect(f"{label}: first completion is not a repeat", completed_body.get("already_completed"), False)
            report.expect(f"{label}: completion records the running version", completed_body.get("first_run", {}).get("version"), base_version)
            completed_at = str(completed_body.get("first_run", {}).get("completed_at") or "")

            after_complete = service.get("/setup/status").json()
            report.expect(f"{label}: status stays complete", after_complete.get("setup_complete"), True)
            report.expect(f"{label}: completion timestamp is reported", after_complete.get("first_run", {}).get("completed_at"), completed_at)
            report.expect(f"{label}: no version change is reported", after_complete.get("first_run", {}).get("version_changed"), False)

        state = _setting(root, "setup.first_run") or {}
        report.expect(f"{label}: completion is persisted", state.get("completed"), True)
        report.check(f"{label}: no override was recorded", "override_required_checks" not in state, str(sorted(state)))
        report.expect(f"{label}: completion is audited", len(_audit(root, "setup.first_run.complete")), 1)
        metadata = _audit(root, "setup.first_run.complete")[0].get("metadata", {})
        report.expect(f"{label}: audit shows no failing required checks", metadata.get("failed_checks"), [])
        report.expect(f"{label}: audit shows no override", metadata.get("override_required_checks"), False)
        return {"root": str(root), "completed_at": completed_at, "base_version": base_version}
    finally:
        _finish_root(root, keep=keep)


def run_ui(repo: Path, python: Path) -> int:
    node = shutil.which("node")
    if node is None:
        print("[FAIL] UI E2E: node is not on PATH", flush=True)
        return 1
    test_file = repo / "app/desktop/tests/setupE2E.test.mjs"
    if not test_file.is_file():
        print(f"[FAIL] UI E2E: {test_file} is missing", flush=True)
        return 1
    env = {key: value for key, value in os.environ.items() if key != "ELECTRON_RUN_AS_NODE"}
    env["CREATOROS_E2E_PYTHON"] = str(python)
    print(f"[run ] UI E2E: {node} --test {test_file}", flush=True)
    completed = subprocess.run([node, "--test", str(test_file)], cwd=repo / "app/desktop", env=env, check=False)
    return completed.returncode


def find_python(repo: Path, override: str | None) -> Path:
    if override:
        candidate = Path(override).resolve()
        if not candidate.is_file():
            raise HarnessError(f"--python does not exist: {candidate}")
        return candidate
    candidates = [
        repo / "app/backend/.venv/Scripts/python.exe",
        repo / "app/backend/.venv/bin/python",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise HarnessError("no backend virtualenv interpreter found; pass --python")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Process-level E2E for the first-run setup wizard")
    parser.add_argument("--repo", default=str(REPO_ROOT), help="repository root")
    parser.add_argument("--python", default=None, help="backend interpreter (defaults to app/backend/.venv)")
    parser.add_argument("--keep", action="store_true", help="keep temporary data roots")
    parser.add_argument("--with-ui", action="store_true", help="also run the Electron desktop E2E")
    parser.add_argument("--skip-backend", action="store_true", help="only run --with-ui")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    try:
        python = find_python(repo, args.python)
        if not args.skip_backend:
            report = Report()
            journeys: list[str] = []
            print(f"repo   : {repo}")
            print(f"python : {python}")
            print("mode   : real 'python -m creatoros serve' process, temporary CREATOROS_ROOT")
            print("")
            try:
                run_override_journey(repo, python, report, keep=args.keep)
                journeys.append("override -> 409 -> override -> restart -> upgrade")
                run_happy_journey(repo, python, report, keep=args.keep)
                journeys.append("present/unverified -> verify -> complete")
            except E2EFailure as exc:
                print("", flush=True)
                print(f"[FAILED] {exc}", flush=True)
                print(report.summary(journeys), flush=True)
                return 1
            print(report.summary(journeys), flush=True)
    except HarnessError as exc:
        print(f"[HARNESS ERROR] {exc}", file=sys.stderr, flush=True)
        return 2

    if args.with_ui:
        ui_code = run_ui(repo, python)
        if ui_code != 0:
            print(f"[FAILED] desktop UI E2E exited {ui_code}", flush=True)
            return 1
        print("[PASS] desktop UI E2E", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
