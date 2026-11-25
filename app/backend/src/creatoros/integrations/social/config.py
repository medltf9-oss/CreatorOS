"""Provider configuration: endpoints, scopes, versions and capability catalogue.

Everything a provider documents that the code needs lives here, so an API version bump or a
scope change is a one-line edit and never a hunt through adapters. Values that must match the
developer app dashboard (client id, redirect URI) are *not* here: they come from the
`provider_app_config` table.

The API versions below are defaults. A Meta app is pinned to a Graph version, so the app
config row can override `api_version`; the default is only used to build a first request.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ...db.enums import AccountType, OAuthRoute, Platform

# Meta versions its Graph API and retires old versions. Keep the default in one place.
DEFAULT_GRAPH_VERSION = "v23.0"
TIKTOK_API_VERSION = "v2"
# Instagram Login uses graph.instagram.com and versions independently of Graph.
DEFAULT_IG_VERSION = "v23.0"

FACEBOOK_AUTHORIZE_URL = "https://www.facebook.com/{version}/dialog/oauth"
FACEBOOK_TOKEN_URL = "https://graph.facebook.com/{version}/oauth/access_token"
FACEBOOK_GRAPH_URL = "https://graph.facebook.com/{version}"
FACEBOOK_DEBUG_URL = "https://graph.facebook.com/{version}/debug_token"

INSTAGRAM_AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
INSTAGRAM_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
INSTAGRAM_LONG_LIVED_URL = "https://graph.instagram.com/access_token"
INSTAGRAM_REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
INSTAGRAM_GRAPH_URL = "https://graph.instagram.com/{version}"

TIKTOK_AUTHORIZE_URL = "https://www.tiktok.com/{version}/auth/authorize/"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/{version}/oauth/token/"
TIKTOK_REVOKE_URL = "https://open.tiktokapis.com/{version}/oauth/revoke/"
TIKTOK_API_URL = "https://open.tiktokapis.com/{version}"
TIKTOK_USER_INFO_URL = "https://open.tiktokapis.com/{version}/user/info/"
TIKTOK_CREATOR_INFO_URL = "https://open.tiktokapis.com/{version}/post/publish/creator_info/query/"

CALLBACK_PATH = "/oauth/callback/{route}"
DEFAULT_CALLBACK_PORT = 47821


@dataclass(frozen=True, slots=True)
class RouteSpec:
    """One login path: which provider it talks to and exactly what it asks for."""

    platform: Platform
    route: OAuthRoute
    label: str
    scopes: tuple[str, ...]
    pkce_required: bool
    pkce_supported: bool
    docs_url: str
    notes: str
    account_types: tuple[AccountType, ...] = field(default_factory=tuple)
    extra_scopes: tuple[str, ...] = field(default_factory=tuple)

FACEBOOK_PAGE_SCOPES = (
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
)

ROUTE_SPECS: dict[tuple[Platform, OAuthRoute], RouteSpec] = {
    (Platform.FACEBOOK, OAuthRoute.FACEBOOK_LOGIN): RouteSpec(
        platform=Platform.FACEBOOK,
        route=OAuthRoute.FACEBOOK_LOGIN,
        label="Facebook Login (Pages)",
        scopes=FACEBOOK_PAGE_SCOPES,
        pkce_required=False,
        # Facebook Login for desktop does not document PKCE; sending a challenge it ignores
        # would be theatre, so the engine does not send one.
        pkce_supported=False,
        docs_url="https://developers.facebook.com/docs/facebook-login/guides/advanced/manual-flow",
        notes=(
            "Only Pages are supported (ADR-10). A personal profile is not connectable through the "
            "Graph API, so it is discovered and reported as unsupported instead of being hidden."
        ),
        account_types=(AccountType.FACEBOOK_PAGE,),
        # `read_insights` is opt-in: Meta requires app review for it and the connection flow does not
        # ask for it, so it is only requested when a user explicitly grants analytics access (P8.20).
        extra_scopes=("read_insights",),
    ),
    (Platform.INSTAGRAM, OAuthRoute.INSTAGRAM_LOGIN): RouteSpec(
        platform=Platform.INSTAGRAM,
        route=OAuthRoute.INSTAGRAM_LOGIN,
        label="Instagram Login (Business Login)",
        scopes=("instagram_business_basic", "instagram_business_content_publish"),
        pkce_required=False,
        pkce_supported=False,
        docs_url="https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login",
        notes=(
            "Path A (ADR-11). Uses instagram_business_* scopes and graph.instagram.com. Works for "
            "professional accounts without a linked Facebook Page."
        ),
        account_types=(AccountType.INSTAGRAM_BUSINESS, AccountType.INSTAGRAM_CREATOR),
        extra_scopes=("instagram_business_manage_insights",),
    ),
    (Platform.INSTAGRAM, OAuthRoute.FACEBOOK_LOGIN): RouteSpec(
        platform=Platform.INSTAGRAM,
        route=OAuthRoute.FACEBOOK_LOGIN,
        label="Instagram via Facebook Login",
        scopes=(
            "instagram_basic",
            "instagram_content_publish",
            "pages_show_list",
            "pages_read_engagement",
        ),
        pkce_required=False,
        pkce_supported=False,
        docs_url="https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user",
        notes=(
            "Path B (ADR-11). Discovers the Instagram professional account linked to a Facebook "
            "Page. Requires a Page, and the account is stored separately from any Instagram Login "
            "account: the two ids are never merged by username."
        ),
        account_types=(AccountType.INSTAGRAM_BUSINESS, AccountType.INSTAGRAM_CREATOR),
        # `instagram_manage_insights` is the Facebook-Login vocabulary for the same read capability;
        # it is opt-in and never requested by the connection flow itself (P8.20).
        extra_scopes=("instagram_manage_insights",),
    ),
    (Platform.TIKTOK, OAuthRoute.TIKTOK_LOGIN): RouteSpec(
        platform=Platform.TIKTOK,
        route=OAuthRoute.TIKTOK_LOGIN,
        label="TikTok Login Kit",
        scopes=("user.info.basic", "video.list", "video.upload"),
        pkce_required=True,
        pkce_supported=True,
        docs_url="https://developers.tiktok.com/doc/oauth-user-access-token-management",
        notes=(
            "Desktop and web apps must use PKCE. video.publish is never requested implicitly: it "
            "is an extra scope the user can opt into, and it only enables Direct Post when TikTok "
            "grants it (ADR-12)."
        ),
        account_types=(AccountType.TIKTOK_CREATOR,),
        # `user.info.stats` is the account-statistics grant (follower/likes/video counters) and is
        # separate from `video.list` (per-video counters). Phase 8 records them independently: an
        # account can authorize video listing and still withhold its counters, and the UI must be
        # able to say which of the two is missing.
        extra_scopes=("video.publish", "user.info.profile", "user.info.stats"),
    ),
}


def route_spec(platform: Platform, route: OAuthRoute) -> RouteSpec:
    try:
        return ROUTE_SPECS[(platform, route)]
    except KeyError as exc:  # pragma: no cover - guarded by API validation
        raise KeyError(f"no route spec for {platform}/{route}") from exc


def routes_for_platform(platform: Platform) -> list[RouteSpec]:
    return [spec for (candidate, _route), spec in ROUTE_SPECS.items() if candidate == platform]


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    """One thing an account may or may not be able to do, and what it needs."""

    key: str
    label: str
    platform: Platform
    routes: tuple[OAuthRoute, ...]
    required_scopes: tuple[str, ...]
    account_types: tuple[AccountType, ...]
    docs_url: str
    manual_only: bool = False
    notes: str = ""
    # `headline=False` means: this capability needs a permission that the connection flow does not
    # request, so it cannot be available yet and must not drag the account's headline state down.
    # It still appears in the list, with its reason.
    headline: bool = True
    # Instagram has two scope vocabularies (`instagram_business_*` for Instagram Login and
    # `instagram_*` for Facebook Login). A capability that both paths can reach therefore needs a
    # per-route scope list; the flat `required_scopes` is the default and the documentation form.
    route_scopes: dict[OAuthRoute, tuple[str, ...]] = field(default_factory=dict)
    # A provider role/task the *account* must also hold, independent of the OAuth grant. A Facebook
    # Page grants a user a task (`ANALYZE`, `CREATE_CONTENT`, ...); holding `read_insights` on the
    # token is not the same as being allowed to read that Page's insights.
    required_page_tasks: tuple[str, ...] = field(default_factory=tuple)

    def scopes_for(self, route: OAuthRoute | None) -> tuple[str, ...]:
        if route is not None and route in self.route_scopes:
            return self.route_scopes[route]
        return self.required_scopes


# The catalogue is data, not logic: Phase 7 asks "is <capability> available for this account?"
# and gets an answer derived from this table plus the account's stored evidence.
CAPABILITY_CATALOGUE: tuple[CapabilityDefinition, ...] = (
    CapabilityDefinition(
        key="facebook.page.read_profile",
        label="Read Page profile",
        platform=Platform.FACEBOOK,
        routes=(OAuthRoute.FACEBOOK_LOGIN,),
        required_scopes=("pages_read_engagement",),
        account_types=(AccountType.FACEBOOK_PAGE,),
        docs_url="https://developers.facebook.com/docs/pages-api",
    ),
    CapabilityDefinition(
        key="facebook.page.publish_text",
        label="Publish text posts to a Page",
        platform=Platform.FACEBOOK,
        routes=(OAuthRoute.FACEBOOK_LOGIN,),
        required_scopes=("pages_manage_posts",),
        account_types=(AccountType.FACEBOOK_PAGE,),
        docs_url="https://developers.facebook.com/docs/pages-api/posts",
    ),
    CapabilityDefinition(
        key="facebook.page.publish_video",
        label="Publish video to a Page",
        platform=Platform.FACEBOOK,
        routes=(OAuthRoute.FACEBOOK_LOGIN,),
        required_scopes=("pages_manage_posts", "pages_read_engagement"),
        account_types=(AccountType.FACEBOOK_PAGE,),
        docs_url="https://developers.facebook.com/docs/video-api/guides/publishing",
    ),
    CapabilityDefinition(
        key="facebook.page.read_insights",
        label="Read Page insights",
        platform=Platform.FACEBOOK,
        routes=(OAuthRoute.FACEBOOK_LOGIN,),
        required_scopes=("read_insights", "pages_read_engagement"),
        account_types=(AccountType.FACEBOOK_PAGE,),
        docs_url="https://developers.facebook.com/docs/graph-api/reference/page/insights",
        headline=False,
        required_page_tasks=("ANALYZE",),
        notes=(
            "read_insights is not part of the connection request: Meta requires app review for it and "
            "CreatorOS only asks for scopes it can act on. The user must also hold the Page's ANALYZE "
            "task, which is a Page role, not an OAuth grant; both must be true before insights is "
            "available (P8.20)."
        ),
    ),
    CapabilityDefinition(
        key="instagram.read_profile",
        label="Read Instagram profile",
        platform=Platform.INSTAGRAM,
        routes=(OAuthRoute.INSTAGRAM_LOGIN, OAuthRoute.FACEBOOK_LOGIN),
        required_scopes=("instagram_business_basic",),
        account_types=(AccountType.INSTAGRAM_BUSINESS, AccountType.INSTAGRAM_CREATOR),
        docs_url="https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user",
        route_scopes={
            OAuthRoute.INSTAGRAM_LOGIN: ("instagram_business_basic",),
            OAuthRoute.FACEBOOK_LOGIN: ("instagram_basic",),
        },
    ),
    CapabilityDefinition(
        key="instagram.publish_reels",
        label="Publish Reels",
        platform=Platform.INSTAGRAM,
        routes=(OAuthRoute.INSTAGRAM_LOGIN, OAuthRoute.FACEBOOK_LOGIN),
        required_scopes=("instagram_business_content_publish",),
        account_types=(AccountType.INSTAGRAM_BUSINESS, AccountType.INSTAGRAM_CREATOR),
        docs_url="https://developers.facebook.com/docs/instagram-platform/content-publishing",
        route_scopes={
            OAuthRoute.INSTAGRAM_LOGIN: ("instagram_business_content_publish",),
            OAuthRoute.FACEBOOK_LOGIN: ("instagram_content_publish",),
        },
    ),
    CapabilityDefinition(
        key="instagram.publish_image",
        label="Publish images",
        platform=Platform.INSTAGRAM,
        routes=(OAuthRoute.INSTAGRAM_LOGIN, OAuthRoute.FACEBOOK_LOGIN),
        required_scopes=("instagram_business_content_publish",),
        account_types=(AccountType.INSTAGRAM_BUSINESS, AccountType.INSTAGRAM_CREATOR),
        docs_url="https://developers.facebook.com/docs/instagram-platform/content-publishing",
        route_scopes={
            OAuthRoute.INSTAGRAM_LOGIN: ("instagram_business_content_publish",),
            OAuthRoute.FACEBOOK_LOGIN: ("instagram_content_publish",),
        },
    ),
    CapabilityDefinition(
        key="instagram.read_insights",
        label="Read Instagram insights",
        platform=Platform.INSTAGRAM,
        routes=(OAuthRoute.INSTAGRAM_LOGIN, OAuthRoute.FACEBOOK_LOGIN),
        required_scopes=("instagram_business_manage_insights",),
        account_types=(AccountType.INSTAGRAM_BUSINESS, AccountType.INSTAGRAM_CREATOR),
        docs_url="https://developers.facebook.com/docs/instagram-platform/insights",
        headline=False,
        route_scopes={
            # The two login paths use different scope vocabularies and must never be merged.
            OAuthRoute.INSTAGRAM_LOGIN: ("instagram_business_manage_insights",),
            OAuthRoute.FACEBOOK_LOGIN: ("instagram_manage_insights",),
        },
        notes=(
            "Insights need an extra permission that the connection flow does not request; it is "
            "reported as unavailable rather than silently omitted. Instagram Login asks for "
            "instagram_business_manage_insights, Facebook Login asks for instagram_manage_insights."
        ),
    ),
    CapabilityDefinition(
        key="tiktok.read_profile",
        label="Read TikTok profile",
        platform=Platform.TIKTOK,
        routes=(OAuthRoute.TIKTOK_LOGIN,),
        required_scopes=("user.info.basic",),
        account_types=(AccountType.TIKTOK_CREATOR,),
        docs_url="https://developers.tiktok.com/doc/tiktok-api-v2-get-user-info",
    ),
    CapabilityDefinition(
        key="tiktok.list_videos",
        label="List own TikTok videos",
        platform=Platform.TIKTOK,
        routes=(OAuthRoute.TIKTOK_LOGIN,),
        required_scopes=("video.list",),
        account_types=(AccountType.TIKTOK_CREATOR,),
        docs_url="https://developers.tiktok.com/doc/tiktok-api-v2-video-list",
    ),
    CapabilityDefinition(
        key="tiktok.upload_draft",
        label="Upload to TikTok inbox (draft)",
        platform=Platform.TIKTOK,
        routes=(OAuthRoute.TIKTOK_LOGIN,),
        required_scopes=("video.upload",),
        account_types=(AccountType.TIKTOK_CREATOR,),
        docs_url="https://developers.tiktok.com/doc/content-posting-api-reference-upload-video",
    ),
    CapabilityDefinition(
        key="tiktok.direct_post",
        label="TikTok Direct Post",
        platform=Platform.TIKTOK,
        routes=(OAuthRoute.TIKTOK_LOGIN,),
        required_scopes=("video.publish",),
        account_types=(AccountType.TIKTOK_CREATOR,),
        docs_url="https://developers.tiktok.com/doc/content-posting-api-reference-direct-post",
        notes=(
            "Requires the Direct Post add-on on the TikTok developer app. Login succeeding never "
            "implies this is allowed."
        ),
    ),
    CapabilityDefinition(
        key="tiktok.read_insights",
        label="Read TikTok account statistics",
        platform=Platform.TIKTOK,
        routes=(OAuthRoute.TIKTOK_LOGIN,),
        # No new TikTok permission is invented: the account statistics grant already exists as
        # `user.info.stats`, separate from `video.list` (per-video counters). If it is already
        # granted, the consent workflow reports that no further consent is needed.
        required_scopes=("user.info.stats",),
        account_types=(AccountType.TIKTOK_CREATOR,),
        docs_url="https://developers.tiktok.com/doc/tiktok-api-v2-get-user-info",
        headline=False,
        notes=(
            "user.info.stats is the account-statistics grant (follower/likes/video counters). It is "
            "separate from video.list, which covers per-video counters; the two are recorded "
            "independently. Everything else (watch time, retention, reach) does not exist in any "
            "approved TikTok API and stays not_supported."
        ),
    ),
    CapabilityDefinition(
        key="manual.export_package",
        label="Manual export package",
        platform=Platform.TIKTOK,
        routes=(OAuthRoute.TIKTOK_LOGIN,),
        required_scopes=(),
        account_types=(AccountType.TIKTOK_CREATOR,),
        docs_url="https://www.tiktok.com/upload",
        manual_only=True,
        notes="Always available: the platform cannot be automated, so a human publishes by hand.",
    ),
)


def capabilities_for(platform: Platform, route: OAuthRoute | None = None) -> list[CapabilityDefinition]:
    """Every capability of a platform.

    Deliberately not filtered by route: a capability that this login path cannot reach must still
    appear, with state `unsupported` and a reason, so "Instagram insights is missing" is visible
    instead of the capability silently disappearing from the list.
    """
    del route
    return [definition for definition in CAPABILITY_CATALOGUE if definition.platform == platform]


def capability_by_key(key: str) -> CapabilityDefinition | None:
    return next((definition for definition in CAPABILITY_CATALOGUE if definition.key == key), None)


#: The capability that answers "may this account read its own analytics?". One per platform, so the
#: consent workflow can resolve it without branching on a provider name in the route layer.
ANALYTICS_CAPABILITY_BY_PLATFORM: dict[Platform, str] = {
    Platform.FACEBOOK: "facebook.page.read_insights",
    Platform.INSTAGRAM: "instagram.read_insights",
    Platform.TIKTOK: "tiktok.read_insights",
}


def analytics_capability(platform: Platform) -> CapabilityDefinition | None:
    key = ANALYTICS_CAPABILITY_BY_PLATFORM.get(platform)
    return capability_by_key(key) if key else None


def facebook_scopes_for_instagram_path_b() -> tuple[str, ...]:
    spec = ROUTE_SPECS[(Platform.INSTAGRAM, OAuthRoute.FACEBOOK_LOGIN)]
    return spec.scopes
