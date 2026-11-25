"""Print why each platform can or cannot publish on this installation.

Reads the same rows `/version` reads: the registered developer app, a connected account, the stored
capability verdict, and - for Instagram - the media delivery bucket. Prints one line per platform and
no secret. Exits 0 always: a missing provider app is a fact about this machine, not a fault.
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    import sqlalchemy as sa

    from creatoros.api.routes.social import get_vault
    from creatoros.db.enums import CapabilityState, ConnectionStatus, Platform
    from creatoros.db.models import AccountCapability, ProviderAppConfig, SocialAccount
    from creatoros.db.session import get_session_factory
    from creatoros.services.publishing.composer import PUBLISH_CAPABILITY
    from creatoros.services.publishing.delivery import DeliveryService

    session = get_session_factory()()
    try:
        delivery = DeliveryService(session, vault=get_vault())
        readiness: dict[str, str] = {}
        for platform in Platform:
            apps = session.execute(
                sa.select(sa.func.count())
                .select_from(ProviderAppConfig)
                .where(ProviderAppConfig.provider == platform.value)
            ).scalar_one()
            if not apps:
                readiness[platform.value] = "no_provider_app_configured"
                continue
            accounts = list(
                session.scalars(
                    sa.select(SocialAccount).where(
                        SocialAccount.platform == platform,
                        SocialAccount.connection_status == ConnectionStatus.CONNECTED,
                    )
                )
            )
            if not accounts:
                readiness[platform.value] = "no_connected_account"
                continue
            capability = session.scalars(
                sa.select(AccountCapability).where(
                    AccountCapability.account_id.in_([account.id for account in accounts]),
                    AccountCapability.capability.in_(sorted(PUBLISH_CAPABILITY.values())),
                )
            ).first()
            if capability is None:
                readiness[platform.value] = "capability_unknown"
            elif capability.state is not CapabilityState.AVAILABLE:
                readiness[platform.value] = str(capability.state)
            elif platform is Platform.INSTAGRAM and not delivery.configured:
                readiness[platform.value] = "no_delivery_bucket"
            else:
                readiness[platform.value] = "available"
        readiness["media_delivery"] = "configured" if delivery.configured else "not_configured"
    finally:
        session.close()
    print(json.dumps(readiness, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
