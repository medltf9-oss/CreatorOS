"""CreatorOS Studio local backend.

Phase 2: persistent data layer, typed API surface, durable queue and worker.
No social publishing exists yet - those routes return 501 with a reason so the
UI can never present a fake success (INV-10).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import sqlalchemy as sa
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.routes import agents as agents_routes
from .api.routes import ai as ai_routes
from .api.routes import analytics as analytics_routes
from .api.routes import content, ops, platform, system
from .api.routes import library as library_routes
from .api.routes import publishing as publishing_routes
from .api.routes import setup as setup_routes
from .api.routes import social as social_routes
from .core.logging import configure_logging
from .core.paths import ensure_dir
from .core.security import ALLOWED_ORIGINS, new_session_token, require_token, security_middleware
from .db.session import get_session_factory
from .services.ai_runtime import set_service_factory

STARTED_AT = time.time()
log = logging.getLogger("creatoros.boot")

ERROR_CODES = {
    400: "VALIDATION_FAILED",
    401: "AUTH_REQUIRED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    422: "VALIDATION_FAILED",
    429: "RATE_LIMITED",
    500: "INTERNAL",
    501: "NOT_IMPLEMENTED",
    503: "DEPENDENCY_UNAVAILABLE",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .core.settings import get_settings

    settings = get_settings()
    configure_logging(settings.log_level, log_dir=ensure_dir("logs", "backend"))

    app.state.session_token = settings.session_token or new_session_token()
    app.state.session_factory = get_session_factory()
    app.state.started_at = STARTED_AT

    # Preflight: a missing/unmigrated database is a hard failure at boot rather
    # than a shower of 500s later.
    app.state.db_ready = False
    try:
        with app.state.session_factory() as session:
            session.execute(sa.text("select 1 from alembic_version limit 1"))
        app.state.db_ready = True
    except sa.exc.SQLAlchemyError as exc:
        log.error("database not migrated: %s", exc)

    for kind in ("logs", "temp", "media_working"):
        ensure_dir(kind)

    # --- Phase 3: AI infrastructure -----------------------------------------
    import os

    from .db.models import AppSetting
    from .integrations.ollama.client import OllamaGateway
    from .services.ai import AiService
    from .services.governor import ResourceGovernor, ResourceLimits
    from .services.models import ModelRegistry

    # Always installed: with no gateway the factory raises AI_UNAVAILABLE at use time,
    # so read-only requests still work and inference requests fail honestly.
    set_service_factory(build_ai_service_for)
    app.state.cancel_registry = {}
    app.state.pulls = {}
    app.state.pull_tasks = {}
    app.state.gateway = None
    app.state.governor = None
    app.state.ai_ready = False

    if os.environ.get("CREATOROS_DISABLE_AI") == "1":
        # Tests and headless tooling run without touching a real model runtime.
        log.info("AI infrastructure disabled by CREATOROS_DISABLE_AI=1")
        with app.state.session_factory() as session:
            _seed_workforce(session)
        _start_social(app)
        workers = _start_workers(app)
        try:
            yield
        finally:
            for worker in workers:
                worker.stop()
            await _stop_social(app)
            log.info("creatoros backend shutting down")
        return

    app.state.gateway = OllamaGateway(settings.ollama_url)

    limits = ResourceLimits()
    with app.state.session_factory() as session:
        row = session.get(AppSetting, "resources.limits")
        if row is not None and row.value_json:
            limits = ResourceLimits.from_dict(row.value_json, base=limits)
        ModelRegistry(session).ensure_seeded()
        interrupted = AiService(session, app.state.gateway, None, None).mark_interrupted()  # type: ignore[arg-type]
        if interrupted:
            log.warning("%d AI execution(s) were interrupted by a previous shutdown", interrupted)
        _seed_workforce(session)

    app.state.governor = ResourceGovernor(app.state.gateway, limits)
    await app.state.governor.start_sampling(interval=3.0)
    app.state.ai_ready = await app.state.gateway.is_available()
    log.info("ollama gateway ready=%s base=%s", app.state.ai_ready, settings.ollama_url)

    _start_social(app)
    workers = _start_workers(app)
    log.info("creatoros backend ready (workers=%d)", len(workers))
    try:
        yield
    finally:
        for worker in workers:
            worker.stop()
        await _stop_social(app)
        for task in list(getattr(app.state, "pull_tasks", {}).values()):
            task.cancel()
        if app.state.governor is not None:
            await app.state.governor.stop_sampling()
        if app.state.gateway is not None:
            await app.state.gateway.aclose()
        log.info("creatoros backend shutting down")


def _vault_state() -> str:
    """The vault is only 'available' when a broker answered, not because it was configured."""
    from .api.routes.social import get_vault

    vault = get_vault()
    return "available" if getattr(vault, "available", False) else "unavailable"


def _callback_state() -> str:
    from .services.social.callback_server import get_listener

    listener = get_listener()
    return "listening" if listener is not None and listener.running else "stopped"


def _capability_state(operation: str) -> str:
    """An operation needs a registered app, a vault and a listener - all three, or it is not available."""
    del operation
    from .db.models import ProviderAppConfig

    if _vault_state() != "available":
        return "vault_unavailable"
    if _callback_state() != "listening":
        return "callback_listener_stopped"
    try:
        with app.state.session_factory() as session:
            configured = session.execute(sa.select(sa.func.count()).select_from(ProviderAppConfig)).scalar_one()
    except sa.exc.SQLAlchemyError:
        return "database_unavailable"
    if not configured:
        return "no_provider_app_configured"
    return "available"


def _publish_platform_state(platform: str) -> str:
    """Why publishing to one platform can or cannot run, from rows rather than from hope.

    Order matters: the first unmet requirement is the one a user can act on.
    """
    from .db.enums import CapabilityState, ConnectionStatus, Platform
    from .db.models import AccountCapability, ProviderAppConfig, SocialAccount
    from .services.publishing.composer import PUBLISH_CAPABILITY
    from .services.publishing.delivery import DeliveryService

    if _vault_state() != "available":
        return "vault_unavailable"
    resolved = Platform(platform)
    try:
        with app.state.session_factory() as session:
            apps = session.execute(
                sa.select(sa.func.count())
                .select_from(ProviderAppConfig)
                .where(ProviderAppConfig.provider == platform)
            ).scalar_one()
            if not apps:
                return "no_provider_app_configured"
            accounts = list(
                session.scalars(
                    sa.select(SocialAccount).where(
                        SocialAccount.platform == resolved,
                        SocialAccount.connection_status == ConnectionStatus.CONNECTED,
                    )
                )
            )
            if not accounts:
                return "no_connected_account"
            capability = session.scalars(
                sa.select(AccountCapability).where(
                    AccountCapability.account_id.in_([account.id for account in accounts]),
                    AccountCapability.capability.in_(sorted(PUBLISH_CAPABILITY.values())),
                )
            ).first()
            if capability is None or capability.state is not CapabilityState.AVAILABLE:
                return str(capability.state) if capability is not None else "capability_unknown"
            if resolved is Platform.INSTAGRAM:
                from .api.routes.social import get_vault

                if not DeliveryService(session, vault=get_vault()).configured:
                    return "no_delivery_bucket"
    except (sa.exc.SQLAlchemyError, ValueError):
        return "database_unavailable"
    return "available"


def _delivery_state() -> str:
    from .api.routes.social import get_vault
    from .services.publishing.delivery import DeliveryService

    try:
        with app.state.session_factory() as session:
            service = DeliveryService(session, vault=get_vault())
            return "configured" if service.configured else "not_configured"
    except (sa.exc.SQLAlchemyError, ValueError):
        return "database_unavailable"


def _analytics_session():
    from .db.session import get_session_factory

    return get_session_factory()()


def _analytics_reports_state() -> str:
    """`available` only when the report table is readable; a missing table is not an optimistic guess."""
    try:
        with _analytics_session() as session:
            from .db.models import AnalyticsReport

            session.execute(sa.select(sa.func.count()).select_from(AnalyticsReport)).scalar()
        return "available"
    except Exception:  # noqa: BLE001 - the capability is the answer, never an exception to the caller
        return "unavailable"


def _analytics_report_formats() -> str:
    from .db.enums import AnalyticsReportFormat

    return ",".join(item.value for item in AnalyticsReportFormat)


def _analytics_retention_state() -> str:
    try:
        with _analytics_session() as session:
            from .services.analytics import retention

            state = retention.status(session, retention.RetentionPolicy(retention_days=365))
        return "never_executed" if state["last_execution"] is None else "available"
    except Exception:  # noqa: BLE001
        return "unavailable"


def _analytics_version_watch_state() -> str:
    try:
        with _analytics_session() as session:
            from .services.analytics import version_watch

            return str(version_watch.load(session)["status"])
    except Exception:  # noqa: BLE001
        return "unavailable"


def _analytics_platform_state(platform: str) -> str:
    """Why analytics for one platform can or cannot collect, from the scopes actually granted."""
    from .db.enums import ConnectionStatus, Platform
    from .db.models import SocialAccount

    try:
        with app.state.session_factory() as session:
            accounts = list(
                session.scalars(
                    sa.select(SocialAccount).where(
                        SocialAccount.platform == Platform(platform),
                        SocialAccount.connection_status == ConnectionStatus.CONNECTED,
                    )
                )
            )
    except sa.exc.SQLAlchemyError:
        return "database_unavailable"
    if not accounts:
        return "no_connected_account"
    scopes = {str(scope) for account in accounts for scope in (account.scopes_json or [])}
    if platform == "facebook":
        return "available" if "read_insights" in scopes else "needs_user_consent"
    if platform == "instagram":
        wanted = {"instagram_manage_insights", "instagram_business_manage_insights"}
        return "available" if scopes & wanted else "requires_additional_permission"
    if platform == "tiktok":
        if "user.info.stats" not in scopes:
            return "requires_scope_user_info_stats"
        if "video.list" not in scopes:
            return "requires_scope_video_list"
        return "available"
    return "unknown"


def _start_social(app: FastAPI) -> None:
    """Install the vault client and start the loopback OAuth callback listener."""
    from .api.routes.social import set_vault
    from .core.settings import get_settings
    from .services.social.callback_server import CallbackListener, set_listener
    from .services.social.vault import vault_from_env

    settings = get_settings()
    vault = vault_from_env()
    set_vault(vault)
    app.state.vault = vault
    if not settings.social_callback_enabled:
        set_listener(None)
        return
    from .api.routes.social import _callback_dispatch

    listener = CallbackListener(
        port=settings.social_callback_port,
        handler=_callback_dispatch(app.state.session_factory, vault),
        loop=asyncio.get_running_loop(),
        logger=log.info,
    )
    try:
        listener.start()
        set_listener(listener)
        app.state.social_listener = listener
    except OSError as error:
        # A busy port is a real limitation, reported rather than retried in a loop.
        log.error("OAuth callback listener could not bind 127.0.0.1:%s (%s)", settings.social_callback_port, error)
        set_listener(None)


async def _stop_social(app: FastAPI) -> None:
    from .services.social.callback_server import set_listener

    listener = getattr(app.state, "social_listener", None)
    if listener is not None:
        listener.stop()
    set_listener(None)
    vault = getattr(app.state, "vault", None)
    if vault is not None:
        await vault.aclose()


def _start_workers(app: FastAPI) -> list:
    """Start the queue worker unless explicitly disabled (tests disable it)."""
    import os

    if os.environ.get("CREATOROS_DISABLE_WORKERS") == "1":
        return []
    from .workers.runner import build_worker

    worker = build_worker(
        app.state.session_factory,
        gateway=getattr(app.state, "gateway", None),
        governor=getattr(app.state, "governor", None),
    )
    worker.start_background(poll_interval=1.0)
    app.state.worker = worker
    return [worker]


def _seed_workforce(session) -> None:
    """Idempotent: departments, 111 agent definitions, tool surface, metric registry."""
    from .services.analytics.agent_grants import apply_analytics_tool_grants
    from .services.analytics.registry import ensure_metric_catalog
    from .services.analytics.workflows import ensure_analytics_prompts
    from .services.approvals import ApprovalService
    from .services.media.agent_grants import apply_media_tool_grants
    from .services.publishing.agent_grants import apply_publishing_tool_grants
    from .services.registry import AgentRegistry
    from .services.social.agent_grants import apply_social_tool_grants
    from .services.toolbox import ensure_tools_seeded

    created = AgentRegistry(session).ensure_seeded()
    tools = ensure_tools_seeded(session)
    granted = apply_media_tool_grants(session)
    if granted.get("agents_updated"):
        log.info("media tools granted to %s agent definitions", granted["agents_updated"])
    social_granted = apply_social_tool_grants(session)
    if social_granted.get("agents_updated"):
        log.info("social tools granted to %s agent definitions", social_granted["agents_updated"])
    publishing_granted = apply_publishing_tool_grants(session)
    if publishing_granted.get("agents_updated"):
        log.info("publishing tools granted to %s agent definitions", publishing_granted["agents_updated"])
    analytics_granted = apply_analytics_tool_grants(session)
    if analytics_granted.get("agents_updated"):
        log.info("analytics tools granted to %s agent definitions", analytics_granted["agents_updated"])
    prompts = ensure_analytics_prompts(session)
    if prompts["created"]:
        log.info("analytics prompt seeds created: %s", len(prompts["created"]))
    if created["agents"] or created["departments"]:
        log.info("workforce seeded: %s departments, %s agents", created["departments"], created["agents"])
    if tools["created"]:
        log.info("tool surface seeded: %s tools", tools["created"])
    # The metric registry is a catalogue, like the agent roster: seeded on boot, idempotent, and
    # never deleting a metric that history still references.
    metrics = ensure_metric_catalog(session)
    if metrics["created"] or metrics["updated"]:
        log.info(
            "metric registry seeded: %s created, %s updated, %s total (catalog %s)",
            metrics["created"],
            metrics["updated"],
            metrics["total"],
            metrics["catalog_version"],
        )
    counts = AgentRegistry(session).counts()
    log.info(
        "workforce ready: %s agents (%s leaders, %s specialists) in %s departments, %s enabled",
        counts["agents_total"],
        counts["leaders"],
        counts["specialists"],
        counts["departments"],
        counts["agents_enabled"],
    )
    expired = ApprovalService(session).expire_stale()
    if expired:
        log.info("%d approval request(s) expired while the app was closed", expired)
    # Commit the seed: the `with session_factory()` block only closes the session, it does not
    # commit. Without this, the metric registry, the Phase 8 grants and the analytics prompt seeds
    # would be rolled back on every boot and the analytics API would see an empty catalogue.
    session.commit()


def build_ai_service_for(session):
    """AiService bound to the running gateway/governor, or a loud failure."""
    from .services.ai import AiService
    from .services.models import ModelRegistry, ModelRouter

    gateway = getattr(app.state, "gateway", None)
    governor = getattr(app.state, "governor", None)
    if gateway is None or governor is None:
        raise RuntimeError(
            "AI_UNAVAILABLE: local inference is disabled (CREATOROS_DISABLE_AI=1 or Ollama missing)"
        )
    router = ModelRouter(ModelRegistry(session), governor, gateway)
    return AiService(session, gateway, governor, router)


app = FastAPI(
    title="CreatorOS Studio Backend",
    version="0.8.0",
    description="Local-only control plane. Loopback only, token authenticated.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(ALLOWED_ORIGINS),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "x-correlation-id"],
    expose_headers=["x-correlation-id", "x-duration-ms"],
    max_age=600,
)
app.middleware("http")(security_middleware)


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """One envelope for every error: `{code, message, correlation_id}`.

    A route may raise with a plain string ("CODE: message") or with a dict
    `{"code": ..., "message": ...}`; both become the same shape.
    """
    body: dict[str, object] = {
        "correlation_id": getattr(request.state, "correlation_id", None),
    }
    if isinstance(exc.detail, dict):
        body["code"] = str(exc.detail.get("code") or ERROR_CODES.get(exc.status_code, "INTERNAL"))
        body["message"] = str(exc.detail.get("message") or "request failed")
        extra = {key: value for key, value in exc.detail.items() if key not in {"code", "message"}}
        if extra:
            body["details"] = extra
    else:
        detail = exc.detail if isinstance(exc.detail, str) else "request failed"
        code = detail.split(":", 1)[0] if ":" in detail and detail.split(":", 1)[0].isupper() else None
        message = detail.split(":", 1)[1].strip() if code and ":" in detail else detail
        body["code"] = code or ERROR_CODES.get(exc.status_code, "INTERNAL")
        body["message"] = message
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Report validation failures in the same envelope as every other error.

    FastAPI's default 422 body is a list of objects, which forces every client to
    special-case it; the UI instead gets one readable message it can display.
    """
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()) if part not in ("body", "query", "path"))
        problems.append(f"{location}: {error.get('msg', 'invalid value')}" if location else str(error.get("msg")))
    return JSONResponse(
        status_code=422,
        content={
            "code": "VALIDATION_ERROR",
            "message": "; ".join(problems) or "request validation failed",
            "correlation_id": getattr(request.state, "correlation_id", None),
            "details": problems,
        },
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "code": "INTERNAL",
            "message": "internal error - see logs/backend/creatoros.log",
            "correlation_id": getattr(request.state, "correlation_id", None),
        },
    )


@app.get("/version", dependencies=[Depends(require_token)])
def version() -> dict[str, object]:
    from .core.settings import get_settings

    settings = get_settings()
    return {
        "name": settings.app_name,
        "version": app.version,
        "api": "phase-8.24",
        "capabilities": {
            "publishing": "available",
            "publish_facebook": _publish_platform_state("facebook"),
            "publish_instagram": _publish_platform_state("instagram"),
            "publish_tiktok": _publish_platform_state("tiktok"),
            "publish_media_delivery": _delivery_state(),
            "publish_scheduler": "available",
            "publish_reconciliation": "available",
            "publish_agent_tools": "available",
            "publish_gui": "available",
            "analytics": "available",
            "analytics_api": "available",
            "analytics_agent_tools": "available",
            "analytics_workflows": "available",
            # P8.20: the permission-upgrade path exists; whether a given platform can be upgraded
            # depends on the account (see analytics_*_insights below).
            "analytics_consent": "available",
            # P8.24: the artefacts and maintenance paths, each with the state the database supports.
            "analytics_reports": _analytics_reports_state(),
            "analytics_reports_formats": _analytics_report_formats(),
            "analytics_retention": _analytics_retention_state(),
            "analytics_version_watch": _analytics_version_watch_state(),
            "analytics_facebook_insights": _analytics_platform_state("facebook"),
            "analytics_instagram_insights": _analytics_platform_state("instagram"),
            "analytics_tiktok": _analytics_platform_state("tiktok"),
            "analytics_version_watch_fetch": "enabled"
            if os.environ.get("CREATOROS_ANALYTICS_VERSION_FETCH", "").strip() == "1"
            else "recorded_table_only",
            "oauth": _capability_state("oauth"),
            "social_accounts": _capability_state("accounts"),
            "social_capabilities": "available",
            "credential_vault": _vault_state(),
            "oauth_callback_listener": _callback_state(),
            "token_refresh": _capability_state("refresh"),
            "video_intake": "available",
            "media_library": "available",
            "media_probe": "available",
            "media_thumbnails": "available",
            "media_waveforms": "available",
            "media_transcode": "available",
            "media_hardware_encoding": "unavailable",
            "media_agent_tools": "available",
            "queue": "available",
            "agents_catalog": "available",
            "agent_registry": "available",
            "agent_orchestration": "available"
            if getattr(app.state, "ai_ready", False)
            else "requires_local_inference",
            "agent_task_graph": "available",
            "agent_tools": "available",
            "agent_memory": "available",
            "human_approvals": "intent_only",
            "local_inference": "available" if getattr(app.state, "ai_ready", False) else "ollama_unavailable",
            "model_management": "available" if getattr(app.state, "ai_ready", False) else "ollama_unavailable",
            "streaming": "available" if getattr(app.state, "ai_ready", False) else "ollama_unavailable",
            "resource_governor": "available",
        },
    }


# Public: liveness only. Everything else requires the per-launch session token.
app.include_router(system.public)
# Media bytes are fetched by <img>/<video>, which cannot send a bearer token. Those routes
# authenticate with a short-lived HMAC signature instead (api/media_access.py) and are
# therefore mounted without the token dependency — never any state-changing route.
app.include_router(library_routes.media_router)
for module in (
    system,
    setup_routes,
    content,
    platform,
    ops,
    ai_routes,
    agents_routes,
    library_routes,
    social_routes,
    publishing_routes,
    analytics_routes,
):
    app.include_router(module.router, dependencies=[Depends(require_token)])
