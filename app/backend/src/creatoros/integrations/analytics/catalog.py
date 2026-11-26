"""The analytics metric catalogue: provider metrics as data, not as logic.

Every row here becomes one `analytics_metric_definition` row, so an observation stored today can be
explained in a year even after the code changed, the provider renamed the metric, or a Graph version
retired it. That is why the provider's own name (`ig_reels_avg_watch_time`) is stored verbatim next
to our stable key (`ig.media.avg_watch_time`): the key is ours to change, the provider name is not.

Sources (read 2026-11-23, recorded per row in `source_doc_url`):

* https://developers.facebook.com/docs/graph-api/reference/insights/
* https://developers.facebook.com/docs/graph-api/reference/video/video_insights/
* https://developers.facebook.com/docs/instagram-platform/insights/
* https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/insights
* https://developers.facebook.com/docs/instagram-platform/api-reference/instagram-user/insights
* https://developers.tiktok.com/doc/tiktok-api-v2-video-list
* https://developers.tiktok.com/doc/tiktok-api-v2-video-object
* https://developers.tiktok.com/doc/tiktok-api-v2-get-user-info

`definition_text` is the provider's documented meaning, condensed for display; the URL is the
authority. Nothing here is invented: a metric that no approved API exposes appears with
`availability_default=not_supported` (or `redacted_by_policy`) rather than being omitted, so the UI
can explain the gap instead of leaving it blank.

Values deliberately *not* in the catalogue because they are not content performance: ad-break
revenue, monetization earnings and demographic breakdowns. Earnings are out of scope for this phase;
demographics are `redacted_by_policy` because the brief forbids collecting unrelated private
information and Phase 8 has no use for them.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from ...db.enums import (
    AnalyticsObjectType,
    MetricAvailability,
    MetricClass,
    MetricPeriod,
    MetricValueKind,
    Platform,
)

CATALOG_VERSION = "2026-11-p8.1"

FB_DOC_INSIGHTS = "https://developers.facebook.com/docs/graph-api/reference/insights/"
FB_DOC_VIDEO = "https://developers.facebook.com/docs/graph-api/reference/video/video_insights/"
IG_DOC_INSIGHTS = "https://developers.facebook.com/docs/instagram-platform/insights/"
IG_DOC_MEDIA = "https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/insights"
IG_DOC_USER = "https://developers.facebook.com/docs/instagram-platform/api-reference/instagram-user/insights"
TT_DOC_VIDEO = "https://developers.tiktok.com/doc/tiktok-api-v2-video-object"
TT_DOC_LIST = "https://developers.tiktok.com/doc/tiktok-api-v2-video-list"
TT_DOC_USER = "https://developers.tiktok.com/doc/tiktok-api-v2-get-user-info"

DAY_WEEK_28 = (MetricPeriod.DAY, MetricPeriod.WEEK, MetricPeriod.DAYS_28)
LIFETIME = (MetricPeriod.LIFETIME,)
INSTANT = (MetricPeriod.INSTANT,)


@dataclass(frozen=True, slots=True)
class MetricSpec:
    key: str
    platform: Platform
    object_type: AnalyticsObjectType
    provider_metric_name: str
    provider_object: str
    source_endpoint: str
    source_doc_url: str
    unit: str
    value_kind: MetricValueKind
    metric_class: MetricClass
    period_kinds: tuple[MetricPeriod, ...]
    definition: str
    #: Either every scope in the tuple, or ``{"any_of": [...]}`` when the provider offers two login
    #: routes that grant the same read capability (Instagram Login vs Facebook Login).
    required_permissions: tuple[str, ...] | dict = ()
    account_threshold: dict = field(default_factory=dict)
    is_estimate: bool = False
    is_deprecated: bool = False
    deprecated_note: str | None = None
    #: ``all_versions`` for a dated retirement, ``api_version`` for a version ceiling.
    deprecation_scope: str | None = None
    api_version_min: str | None = None
    api_version_max: str | None = None
    availability_default: MetricAvailability = MetricAvailability.NOT_COLLECTED
    preferred_for: str | None = None
    supersedes: str | None = None

    @property
    def is_scalar(self) -> bool:
        return self.value_kind is MetricValueKind.SCALAR


def _fb_page(
    key: str,
    name: str,
    *,
    unit: str = "count",
    cls: MetricClass = MetricClass.PERIOD,
    periods: tuple[MetricPeriod, ...] = DAY_WEEK_28,
    definition: str,
    **kwargs,
) -> MetricSpec:
    return MetricSpec(
        key=key,
        platform=Platform.FACEBOOK,
        object_type=AnalyticsObjectType.ACCOUNT,
        provider_metric_name=name,
        provider_object="page",
        source_endpoint="/{version}/{page-id}/insights",
        source_doc_url=FB_DOC_INSIGHTS,
        unit=unit,
        value_kind=MetricValueKind.SCALAR,
        metric_class=cls,
        period_kinds=periods,
        definition=definition,
        required_permissions=("read_insights", "pages_read_engagement"),
        account_threshold={"min_page_likes": 100},
        **kwargs,
    )


def _fb_post(
    key: str,
    name: str,
    *,
    unit: str = "count",
    cls: MetricClass = MetricClass.CUMULATIVE,
    periods: tuple[MetricPeriod, ...] = LIFETIME,
    definition: str,
    endpoint: str = "/{version}/{post-id}/insights",
    kind: MetricValueKind = MetricValueKind.SCALAR,
    **kwargs,
) -> MetricSpec:
    return MetricSpec(
        key=key,
        platform=Platform.FACEBOOK,
        object_type=AnalyticsObjectType.POST,
        provider_metric_name=name,
        provider_object="post",
        source_endpoint=endpoint,
        source_doc_url=FB_DOC_INSIGHTS,
        unit=unit,
        value_kind=kind,
        metric_class=cls,
        period_kinds=periods,
        definition=definition,
        required_permissions=("read_insights", "pages_read_engagement"),
        account_threshold={"min_page_likes": 100},
        **kwargs,
    )


def _fb_video(
    key: str,
    name: str,
    *,
    unit: str = "count",
    cls: MetricClass = MetricClass.CUMULATIVE,
    periods: tuple[MetricPeriod, ...] = LIFETIME,
    definition: str,
    kind: MetricValueKind = MetricValueKind.SCALAR,
    **kwargs,
) -> MetricSpec:
    return MetricSpec(
        key=key,
        platform=Platform.FACEBOOK,
        object_type=AnalyticsObjectType.VIDEO,
        provider_metric_name=name,
        provider_object="video",
        source_endpoint="/{version}/{video-id}/video_insights",
        source_doc_url=FB_DOC_VIDEO,
        unit=unit,
        value_kind=kind,
        metric_class=cls,
        period_kinds=periods,
        definition=definition,
        required_permissions=("read_insights", "pages_manage_engagement"),
        account_threshold={"min_page_likes": 100},
        **kwargs,
    )


def _fb_reel(key: str, name: str, *, definition: str, **kwargs) -> MetricSpec:
    spec = _fb_video(key, name, definition=definition, **kwargs)
    return replace(spec, object_type=AnalyticsObjectType.REEL)


FB_PAGE: tuple[MetricSpec, ...] = (
    MetricSpec(
        key="fb.page.followers_total",
        platform=Platform.FACEBOOK,
        object_type=AnalyticsObjectType.ACCOUNT,
        provider_metric_name="followers_count",
        provider_object="page",
        source_endpoint="/{version}/{page-id}",
        source_doc_url="https://developers.facebook.com/docs/graph-api/reference/page/",
        unit="count",
        value_kind=MetricValueKind.SCALAR,
        metric_class=MetricClass.POINT_IN_TIME,
        period_kinds=INSTANT,
        definition="Current number of people following the Page. Distinct from Page likes.",
        required_permissions=("pages_read_engagement",),
        preferred_for="followers",
    ),
    MetricSpec(
        key="fb.page.likes_total",
        platform=Platform.FACEBOOK,
        object_type=AnalyticsObjectType.ACCOUNT,
        provider_metric_name="fan_count",
        provider_object="page",
        source_endpoint="/{version}/{page-id}",
        source_doc_url="https://developers.facebook.com/docs/graph-api/reference/page/",
        unit="count",
        value_kind=MetricValueKind.SCALAR,
        metric_class=MetricClass.POINT_IN_TIME,
        period_kinds=INSTANT,
        definition="Current number of Page likes. Facebook counts likes and follows separately.",
        required_permissions=("pages_read_engagement",),
    ),
    _fb_page(
        "fb.page.media_views",
        "page_media_view",
        definition="Number of times Page content (videos, posts, stories, ads) was played or displayed.",
        preferred_for="reach",
    ),
    _fb_page(
        "fb.page.media_viewers_unique",
        "page_total_media_view_unique",
        definition="Number of unique media viewers for the Page. Meta labels unique values as estimates.",
        is_estimate=True,
        preferred_for="reach_unique",
    ),
    _fb_page(
        "fb.page.impressions",
        "page_impressions",
        definition="Number of times Page content or content about the Page appeared on screen.",
        is_deprecated=True,
        deprecated_note="Retired for all API versions in June 2026; use page_media_view.",
        deprecation_scope="all_versions",
    ),
    _fb_page(
        "fb.page.impressions_unique",
        "page_impressions_unique",
        definition="Number of people who saw Page content at least once. Estimated by Meta.",
        is_estimate=True,
        is_deprecated=True,
        deprecated_note="Unavailable above Graph v25 and retired in June 2026.",
        api_version_max="v24.0",
        deprecation_scope="api_version",
    ),
    _fb_page(
        "fb.page.engagements",
        "page_post_engagements",
        definition="Number of times people engaged with Page posts: reactions, comments, shares and more.",
    ),
    _fb_page(
        "fb.page.reactions_like",
        "page_actions_post_reactions_like_total",
        definition="Daily total of like (and care) reactions on the Page's posts.",
    ),
    _fb_page(
        "fb.page.reactions_love",
        "page_actions_post_reactions_love_total",
        definition="Daily total of love reactions on the Page's posts.",
    ),
    _fb_page(
        "fb.page.reactions_wow",
        "page_actions_post_reactions_wow_total",
        definition="Daily total of wow reactions on the Page's posts.",
    ),
    _fb_page(
        "fb.page.reactions_haha",
        "page_actions_post_reactions_haha_total",
        definition="Daily total of haha reactions on the Page's posts.",
    ),
    _fb_page(
        "fb.page.reactions_sorry",
        "page_actions_post_reactions_sorry_total",
        definition="Daily total of sad reactions on the Page's posts.",
    ),
    _fb_page(
        "fb.page.reactions_anger",
        "page_actions_post_reactions_anger_total",
        definition="Daily total of angry reactions on the Page's posts.",
    ),
    _fb_page(
        "fb.page.follows",
        "page_follows",
        definition="Number of people following the Page.",
        periods=(MetricPeriod.DAY,),
    ),
    _fb_page(
        "fb.page.follows_new",
        "page_daily_follows_unique",
        definition="Number of accounts that started following the Page in the period. Estimated.",
        periods=(MetricPeriod.DAY, MetricPeriod.WEEK, MetricPeriod.DAYS_28),
        is_estimate=True,
    ),
    _fb_page(
        "fb.page.unfollows",
        "page_daily_unfollows_unique",
        definition="Number of accounts that stopped following the Page in the period. Estimated.",
        is_estimate=True,
    ),
    _fb_page(
        "fb.page.likes_new",
        "page_fan_adds_unique",
        definition="Number of accounts that liked the Page for the first time. Estimated.",
        periods=(MetricPeriod.DAY, MetricPeriod.WEEK, MetricPeriod.DAYS_28),
        is_estimate=True,
    ),
    _fb_page(
        "fb.page.likes_removed",
        "page_fan_removes_unique",
        definition="Number of accounts that removed their like from the Page. Estimated.",
        is_estimate=True,
    ),
    _fb_page(
        "fb.page.video_views",
        "page_video_views",
        definition="Number of times Page videos played for at least 3 seconds, replays excluded.",
        preferred_for="views",
    ),
    _fb_page(
        "fb.page.video_views_organic",
        "page_video_views_organic",
        definition="Video views from organic reach.",
    ),
    _fb_page(
        "fb.page.video_view_time",
        "page_video_view_time",
        unit="ms",
        cls=MetricClass.PERIOD,
        periods=(MetricPeriod.DAY,),
        definition="Total time in milliseconds people spent watching Page videos.",
        preferred_for="watch_time",
    ),
    _fb_page(
        "fb.page.video_complete_30s",
        "page_video_complete_views_30s",
        definition="Video plays of at least 30 seconds, or nearly the whole video if shorter.",
    ),
    _fb_page(
        "fb.page.video_repeat_views",
        "page_video_repeat_views",
        definition="Number of times Page videos were replayed for at least 3 seconds.",
    ),
    _fb_page(
        "fb.page.video_views_unique",
        "page_video_views_unique",
        definition="Number of people who watched Page videos for at least 3 seconds. Estimated.",
        is_estimate=True,
        is_deprecated=True,
        deprecation_scope="api_version",
        deprecated_note="Unavailable above Graph v25 and retired in June 2026.",
        api_version_max="v24.0",
    ),
    _fb_page(
        "fb.page.profile_views",
        "page_views_total",
        definition="Number of times the Page profile was viewed by logged-in and logged-out people.",
    ),
)

FB_POST: tuple[MetricSpec, ...] = (
    _fb_post(
        "fb.post.media_views",
        "post_media_view",
        definition="Number of times the post's content was played or displayed.",
        preferred_for="reach",
    ),
    _fb_post(
        "fb.post.media_viewers_unique",
        "post_total_media_view_unique",
        definition="Number of unique media viewers for the post. Estimated.",
        is_estimate=True,
    ),
    _fb_post(
        "fb.post.impressions",
        "post_impressions",
        definition="Number of times the post appeared on screen.",
        is_deprecated=True,
        deprecation_scope="api_version",
        deprecated_note="Deprecated from Graph v22.0 and retired in June 2026; use post_media_view.",
        api_version_max="v21.0",
    ),
    _fb_post(
        "fb.post.impressions_unique",
        "post_impressions_unique",
        definition="Number of people the post appeared to. Estimated.",
        is_estimate=True,
        is_deprecated=True,
        deprecation_scope="api_version",
        deprecated_note="Deprecated from Graph v22.0 and retired in June 2026.",
        api_version_max="v21.0",
    ),
    _fb_post(
        "fb.post.reactions_like",
        "post_reactions_like_total",
        definition="Number of like reactions on the post.",
    ),
    _fb_post(
        "fb.post.reactions_love",
        "post_reactions_love_total",
        definition="Number of love reactions on the post.",
    ),
    _fb_post(
        "fb.post.reactions_wow",
        "post_reactions_wow_total",
        definition="Number of wow reactions on the post.",
    ),
    _fb_post(
        "fb.post.reactions_haha",
        "post_reactions_haha_total",
        definition="Number of haha reactions on the post.",
    ),
    _fb_post(
        "fb.post.reactions_sorry",
        "post_reactions_sorry_total",
        definition="Number of sad reactions on the post.",
    ),
    _fb_post(
        "fb.post.reactions_anger",
        "post_reactions_anger_total",
        definition="Number of angry reactions on the post.",
    ),
    _fb_post(
        "fb.post.reactions_total",
        "reactions",
        definition="Total reactions on the post, read from the Post node's reactions summary.",
        endpoint="/{version}/{post-id}?fields=reactions.summary(true).limit(0)",
    ),
    _fb_post(
        "fb.post.comments",
        "comments",
        definition="Total comments on the post, read from the Post node's comments summary.",
        endpoint="/{version}/{post-id}?fields=comments.summary(true).limit(0)",
    ),
    _fb_post(
        "fb.post.shares",
        "shares",
        definition="Total shares of the post, read from the Post node's shares field.",
        endpoint="/{version}/{post-id}?fields=shares",
    ),
    _fb_post(
        "fb.post.clicks",
        "post_clicks",
        definition="Number of clicks anywhere on the post that did not create a story.",
    ),
    _fb_post(
        "fb.post.clicks_by_type",
        "post_clicks_by_type",
        kind=MetricValueKind.BREAKDOWN,
        definition="Post clicks broken down by consumption type.",
    ),
    _fb_post(
        "fb.post.video_views",
        "post_video_views",
        definition="Number of times the post's video played for at least 3 seconds, replays excluded.",
        preferred_for="views",
    ),
    _fb_post(
        "fb.post.video_views_15s",
        "post_video_views_15s",
        definition="Number of times the video played for at least 15 seconds.",
    ),
    _fb_post(
        "fb.post.video_view_time",
        "post_video_view_time",
        unit="ms",
        definition="Total time in milliseconds the video was watched, including replays.",
        preferred_for="watch_time",
    ),
    _fb_post(
        "fb.post.video_avg_watch_time",
        "post_video_avg_time_watched",
        unit="ms",
        cls=MetricClass.CUMULATIVE,
        definition="Average time in milliseconds viewers watched the video. Zero for reshared videos.",
        preferred_for="avg_watch_time",
    ),
    _fb_post(
        "fb.post.video_length",
        "post_video_length",
        unit="ms",
        definition="Duration of the video post as reported by the provider.",
    ),
    _fb_post(
        "fb.post.video_complete_views",
        "post_video_complete_views_organic",
        definition="Number of times the video played to at least 97% of its length, organic reach.",
    ),
    _fb_post(
        "fb.post.video_retention_curve",
        "post_video_retention_graph",
        unit="ratio",
        kind=MetricValueKind.SERIES,
        cls=MetricClass.SERIES,
        definition="Plays at each of 40 equal intervals as a percentage of all plays.",
        preferred_for="retention",
    ),
    _fb_post(
        "fb.post.video_social_actions",
        "post_video_social_actions_count_unique",
        definition="Unique social actions (reactions, comments, shares) on the video post. Estimated.",
        is_estimate=True,
    ),
    _fb_post(
        "fb.post.video_followers",
        "post_video_followers",
        definition="Number of follows attributed to the video post.",
    ),
)

FB_VIDEO: tuple[MetricSpec, ...] = (
    _fb_video(
        "fb.video.views",
        "total_video_views",
        definition="Number of times the video played for at least 3 seconds, replays excluded.",
        preferred_for="views",
    ),
    _fb_video(
        "fb.video.views_unique",
        "total_video_views_unique",
        definition="Number of people who watched the video for at least 3 seconds. Estimated.",
        is_estimate=True,
        is_deprecated=True,
        deprecated_note="Retired for all API versions in June 2026.",
        deprecation_scope="all_versions",
    ),
    _fb_video(
        "fb.video.views_autoplayed",
        "total_video_views_autoplayed",
        definition="Views that started automatically.",
    ),
    _fb_video(
        "fb.video.views_clicked_to_play",
        "total_video_views_clicked_to_play",
        definition="Views that started after a click on play.",
    ),
    _fb_video(
        "fb.video.views_sound_on",
        "total_video_views_sound_on",
        definition="Views with sound enabled.",
    ),
    _fb_video(
        "fb.video.complete_views",
        "total_video_complete_views",
        definition="Number of times the video played to at least 97% of its length.",
    ),
    _fb_video(
        "fb.video.views_10s",
        "total_video_10s_views",
        definition="Number of times the video played for at least 10 seconds.",
    ),
    _fb_video(
        "fb.video.views_15s",
        "total_video_15s_views",
        definition="Number of times the video played for at least 15 seconds.",
    ),
    _fb_video(
        "fb.video.views_60s",
        "total_video_60s_excludes_shorter_views",
        definition="Number of times the video played for at least 60 seconds (videos of 60s or longer).",
    ),
    _fb_video(
        "fb.video.view_total_time",
        "total_video_view_total_time",
        unit="ms",
        definition="Total time in milliseconds people watched the video.",
        preferred_for="watch_time",
    ),
    _fb_video(
        "fb.video.avg_time_watched",
        "total_video_avg_time_watched",
        unit="ms",
        definition="Average time in milliseconds people watched the video.",
        preferred_for="avg_watch_time",
    ),
    _fb_video(
        "fb.video.retention_curve",
        "total_video_retention_graph",
        unit="ratio",
        kind=MetricValueKind.SERIES,
        cls=MetricClass.SERIES,
        definition="Plays at each of 40 equal intervals as a percentage of all views.",
        preferred_for="retention",
    ),
    _fb_video(
        "fb.video.reactions_by_type",
        "total_video_reactions_by_type_total",
        kind=MetricValueKind.BREAKDOWN,
        definition="Reactions on the video, broken down by reaction type.",
    ),
    _fb_reel(
        "fb.reel.plays",
        "blue_reels_play_count",
        definition="Number of times the reel started to play after an impression; replays excluded.",
        preferred_for="views",
    ),
    _fb_reel(
        "fb.reel.total_plays",
        "fb_reels_total_plays",
        definition="Number of reel plays including replays within the same session.",
    ),
    _fb_reel(
        "fb.reel.replays",
        "fb_reels_replay_count",
        definition="Number of times the reel started again after the initial play.",
        preferred_for="replays",
    ),
    _fb_reel(
        "fb.reel.avg_watch_time",
        "post_video_avg_time_watched",
        unit="ms",
        definition="Average play time per instance, including replays, so it can exceed the reel length.",
        preferred_for="avg_watch_time",
    ),
    _fb_reel(
        "fb.reel.view_time",
        "post_video_view_time",
        unit="ms",
        definition="Total milliseconds the reel played, including replays.",
        preferred_for="watch_time",
    ),
    _fb_reel(
        "fb.reel.retention_curve",
        "post_video_retention_graph",
        unit="ratio",
        kind=MetricValueKind.SERIES,
        cls=MetricClass.SERIES,
        definition="Percentage of plays at each timestamp segment of the reel.",
        preferred_for="retention",
    ),
    _fb_reel(
        "fb.reel.followers",
        "post_video_followers",
        definition="Number of follows for the reel.",
    ),
    _fb_reel(
        "fb.reel.likes",
        "post_video_likes_by_reaction_type",
        kind=MetricValueKind.BREAKDOWN,
        definition="Likes on the reel, broken down by reaction type.",
    ),
    _fb_reel(
        "fb.reel.social_actions",
        "post_video_social_actions",
        definition="Number of comments and shares on the reel.",
    ),
    _fb_reel(
        "fb.reel.viewers_unique",
        "post_impressions_unique",
        definition="Number of people who saw the reel at least once. Estimated.",
        is_estimate=True,
        is_deprecated=True,
        deprecation_scope="all_versions",
        deprecated_note="Retired for all API versions in June 2026.",
    ),
)


#: Instagram exposes these metrics through two *alternative* login routes. Requiring all five
#: scopes would mark every Instagram metric as unauthorized forever.
IG_BUSINESS_LOGIN_SCOPES = ("instagram_business_basic", "instagram_business_manage_insights")
IG_FACEBOOK_LOGIN_SCOPES = ("instagram_basic", "instagram_manage_insights", "pages_read_engagement")
IG_PERMISSIONS: dict = {"any_of": [list(IG_BUSINESS_LOGIN_SCOPES), list(IG_FACEBOOK_LOGIN_SCOPES)]}


def _ig_account(key: str, name: str, *, definition: str, **kwargs) -> MetricSpec:
    return MetricSpec(
        key=key,
        platform=Platform.INSTAGRAM,
        object_type=AnalyticsObjectType.ACCOUNT,
        provider_metric_name=name,
        provider_object="ig_user",
        source_endpoint="/{version}/{ig-user-id}/insights",
        source_doc_url=IG_DOC_USER,
        unit=kwargs.pop("unit", "count"),
        value_kind=kwargs.pop("kind", MetricValueKind.SCALAR),
        metric_class=kwargs.pop("cls", MetricClass.PERIOD),
        period_kinds=kwargs.pop("periods", (MetricPeriod.DAY,)),
        definition=definition,
        required_permissions=IG_PERMISSIONS,
        **{"account_threshold": {"min_followers": 100}, **kwargs},
    )


def _ig_media(key: str, name: str, *, definition: str, **kwargs) -> MetricSpec:
    return MetricSpec(
        key=key,
        platform=Platform.INSTAGRAM,
        object_type=kwargs.pop("object_type", AnalyticsObjectType.POST),
        provider_metric_name=name,
        provider_object="ig_media",
        source_endpoint="/{version}/{ig-media-id}/insights",
        source_doc_url=IG_DOC_MEDIA,
        unit=kwargs.pop("unit", "count"),
        value_kind=kwargs.pop("kind", MetricValueKind.SCALAR),
        metric_class=kwargs.pop("cls", MetricClass.CUMULATIVE),
        period_kinds=kwargs.pop("periods", LIFETIME),
        definition=definition,
        **{"required_permissions": IG_PERMISSIONS, **kwargs},
    )


IG_ACCOUNT: tuple[MetricSpec, ...] = (
    MetricSpec(
        key="ig.account.followers",
        platform=Platform.INSTAGRAM,
        object_type=AnalyticsObjectType.ACCOUNT,
        provider_metric_name="followers_count",
        provider_object="ig_user",
        source_endpoint="/{version}/{ig-user-id}?fields=followers_count,follows_count,media_count",
        source_doc_url=IG_DOC_INSIGHTS,
        unit="count",
        value_kind=MetricValueKind.SCALAR,
        metric_class=MetricClass.POINT_IN_TIME,
        period_kinds=INSTANT,
        definition="Current number of accounts following the Instagram professional account.",
        # `followers_count` is profile metadata: it needs the basic grant, not the insights grant.
        required_permissions={"any_of": [["instagram_business_basic"], ["instagram_basic"]]},
        preferred_for="followers",
    ),
    MetricSpec(
        key="ig.account.follows_count",
        platform=Platform.INSTAGRAM,
        object_type=AnalyticsObjectType.ACCOUNT,
        provider_metric_name="follows_count",
        provider_object="ig_user",
        source_endpoint="/{version}/{ig-user-id}?fields=follows_count",
        source_doc_url=IG_DOC_INSIGHTS,
        unit="count",
        value_kind=MetricValueKind.SCALAR,
        metric_class=MetricClass.POINT_IN_TIME,
        period_kinds=INSTANT,
        definition="Current number of accounts this professional account follows.",
        required_permissions={"any_of": [["instagram_business_basic"], ["instagram_basic"]]},
    ),
    MetricSpec(
        key="ig.account.media_count",
        platform=Platform.INSTAGRAM,
        object_type=AnalyticsObjectType.ACCOUNT,
        provider_metric_name="media_count",
        provider_object="ig_user",
        source_endpoint="/{version}/{ig-user-id}?fields=media_count",
        source_doc_url=IG_DOC_INSIGHTS,
        unit="count",
        value_kind=MetricValueKind.SCALAR,
        metric_class=MetricClass.POINT_IN_TIME,
        period_kinds=INSTANT,
        definition="Current number of media objects on the account.",
        required_permissions={"any_of": [["instagram_business_basic"], ["instagram_basic"]]},
    ),
    _ig_account(
        "ig.account.views",
        "views",
        definition="Number of times the account's content was played or displayed.",
        preferred_for="views",
        supersedes="ig.account.impressions",
    ),
    _ig_account(
        "ig.account.reach",
        "reach",
        definition="Number of unique accounts that saw the account's content at least once. Estimated.",
        is_estimate=True,
        preferred_for="reach",
    ),
    _ig_account(
        "ig.account.impressions",
        "impressions",
        definition="Number of times the account's content appeared on screen.",
        is_deprecated=True,
        deprecated_note="Deprecated for v22.0+ and for all versions from 2025-04-21; use views.",
        api_version_max="v21.0",
        deprecation_scope="api_version",
    ),
    _ig_account(
        "ig.account.accounts_engaged",
        "accounts_engaged",
        definition="Number of accounts that interacted with the account's content. Estimated.",
        is_estimate=True,
    ),
    _ig_account(
        "ig.account.total_interactions",
        "total_interactions",
        definition="Total post, story, reel and video interactions, including boosted content.",
    ),
    _ig_account("ig.account.likes", "likes", definition="Likes on the account's posts, reels and videos."),
    _ig_account("ig.account.comments", "comments", definition="Comments on the account's posts, reels and videos."),
    _ig_account("ig.account.saves", "saves", definition="Saves of the account's posts, reels and videos."),
    _ig_account("ig.account.shares", "shares", definition="Shares of the account's content."),
    _ig_account("ig.account.replies", "replies", definition="Story replies, including text and quick reactions."),
    _ig_account("ig.account.reposts", "reposts", definition="Reposts of the account's posts, stories, reels and videos."),
    _ig_account(
        "ig.account.follows_unfollows",
        "follows_and_unfollows",
        definition="Accounts that followed and unfollowed in the period, broken down by follow type.",
        kind=MetricValueKind.BREAKDOWN,
        cls=MetricClass.SERIES,
        # `total_value` in the API: a lifetime breakdown, not a daily time series.
        periods=(MetricPeriod.LIFETIME,),
        account_threshold={"min_followers": 100},
        # Not `followers`: this is the *change* breakdown, the headline count is ig.account.followers.
        preferred_for="follower_changes",
    ),
    _ig_account(
        "ig.account.profile_link_taps",
        "profile_links_taps",
        definition="Taps on the business address, call, email and text buttons.",
        kind=MetricValueKind.BREAKDOWN,
        cls=MetricClass.SERIES,
        periods=(MetricPeriod.LIFETIME,),
    ),
    _ig_account(
        "ig.account.follower_demographics",
        "follower_demographics",
        definition="Demographic characteristics of followers (age, city, country, gender).",
        kind=MetricValueKind.BREAKDOWN,
        cls=MetricClass.SERIES,
        periods=(MetricPeriod.LIFETIME,),
        availability_default=MetricAvailability.REDACTED_BY_POLICY,
    ),
    _ig_account(
        "ig.account.engaged_demographics",
        "engaged_audience_demographics",
        definition="Demographic characteristics of the engaged audience.",
        kind=MetricValueKind.BREAKDOWN,
        cls=MetricClass.SERIES,
        periods=(MetricPeriod.LIFETIME,),
        availability_default=MetricAvailability.REDACTED_BY_POLICY,
    ),
)

IG_MEDIA: tuple[MetricSpec, ...] = (
    _ig_media("ig.media.views", "views", definition="Times the media was played on Instagram.", preferred_for="views"),
    _ig_media(
        "ig.media.reach",
        "reach",
        definition="Number of unique accounts that saw the media at least once. Estimated.",
        is_estimate=True,
        preferred_for="reach",
    ),
    _ig_media("ig.media.likes", "likes", definition="Number of likes on the media."),
    _ig_media("ig.media.comments", "comments", definition="Number of comments on the media."),
    _ig_media("ig.media.saved", "saved", definition="Number of times the media was saved.", preferred_for="saves"),
    _ig_media("ig.media.shares", "shares", definition="Number of shares of the media."),
    _ig_media("ig.media.reposts", "reposts", definition="Number of reposts of the media."),
    _ig_media(
        "ig.media.total_interactions",
        "total_interactions",
        definition="Likes, saves, comments and shares minus unlikes, unsaves and deleted comments.",
    ),
    _ig_media(
        "ig.media.impressions",
        "impressions",
        definition="Number of times the media was seen.",
        is_deprecated=True,
        deprecated_note="Errors for media created on or after 2024-07-02; use views.",
        api_version_max="v21.0",
        deprecation_scope="api_version",
    ),
    _ig_media(
        "ig.media.avg_watch_time",
        "ig_reels_avg_watch_time",
        object_type=AnalyticsObjectType.REEL,
        unit="ms",
        definition="Average time spent playing the reel. Reels only.",
        preferred_for="avg_watch_time",
    ),
    _ig_media(
        "ig.media.view_total_time",
        "ig_reels_video_view_total_time",
        object_type=AnalyticsObjectType.REEL,
        unit="ms",
        definition="Total time the reel was played, including replays. Reels only; in development.",
        preferred_for="watch_time",
    ),
    _ig_media(
        "ig.media.skip_rate",
        "reels_skip_rate",
        object_type=AnalyticsObjectType.REEL,
        unit="ratio",
        cls=MetricClass.RATIO,
        definition="Share of views that skipped within the first 3 seconds. Estimated, in development.",
        is_estimate=True,
    ),
    _ig_media("ig.media.profile_visits", "profile_visits", definition="Times the profile was visited from the media."),
    _ig_media(
        "ig.media.follows",
        "follows",
        definition="Number of accounts that followed from the media.",
        object_type=AnalyticsObjectType.POST,
    ),
    _ig_media(
        "ig.media.facebook_views",
        "facebook_views",
        definition="Times the media was played on Facebook. Errors when the media was not crossposted.",
    ),
    _ig_media(
        "ig.media.crossposted_views",
        "crossposted_views",
        object_type=AnalyticsObjectType.REEL,
        definition="Views across Instagram and Facebook for a crossposted reel.",
    ),
    _ig_media(
        "ig.media.total_likes",
        "total_likes",
        definition="Likes across all surfaces including promoted media. Facebook Login only.",
        required_permissions={"any_of": [list(IG_FACEBOOK_LOGIN_SCOPES)]},
    ),
    _ig_media(
        "ig.media.total_comments",
        "total_comments",
        definition="Comments across all surfaces including promoted media. Facebook Login only.",
        required_permissions={"any_of": [list(IG_FACEBOOK_LOGIN_SCOPES)]},
    ),
    _ig_media(
        "ig.media.total_views",
        "total_views",
        definition="Views across all surfaces including promoted media. Facebook Login only.",
        required_permissions={"any_of": [list(IG_FACEBOOK_LOGIN_SCOPES)]},
    ),
    _ig_media(
        "ig.media.link_clicks",
        "link_clicks",
        object_type=AnalyticsObjectType.STORY,
        definition="Taps on links in the story. Stories only.",
    ),
    _ig_media(
        "ig.media.replies",
        "replies",
        object_type=AnalyticsObjectType.STORY,
        definition="Replies to the story. Stories only, available for 24 hours.",
    ),
)


TIKTOK_STATS = "user.info.stats"
TIKTOK_BASIC = "user.info.basic"
TIKTOK_VIDEO_LIST = "video.list"


def _tt_account(key: str, name: str, *, definition: str, scope: str, **kwargs) -> MetricSpec:
    return MetricSpec(
        key=key,
        platform=Platform.TIKTOK,
        object_type=AnalyticsObjectType.ACCOUNT,
        provider_metric_name=name,
        provider_object="tiktok_user",
        source_endpoint="/v2/user/info/",
        source_doc_url=TT_DOC_USER,
        unit="count",
        value_kind=MetricValueKind.SCALAR,
        metric_class=MetricClass.POINT_IN_TIME,
        period_kinds=INSTANT,
        definition=definition,
        required_permissions=(scope,),
        **kwargs,
    )


def _tt_video(key: str, name: str, *, definition: str, unit: str = "count", cls: MetricClass = MetricClass.CUMULATIVE, kind: MetricValueKind = MetricValueKind.SCALAR, **kwargs) -> MetricSpec:
    return MetricSpec(
        key=key,
        platform=Platform.TIKTOK,
        object_type=AnalyticsObjectType.VIDEO,
        provider_metric_name=name,
        provider_object="tiktok_video",
        source_endpoint="/v2/video/list/",
        source_doc_url=TT_DOC_VIDEO,
        unit=unit,
        value_kind=kind,
        metric_class=cls,
        period_kinds=LIFETIME,
        definition=definition,
        required_permissions=(TIKTOK_VIDEO_LIST,),
        **kwargs,
    )


TIKTOK: tuple[MetricSpec, ...] = (
    _tt_account(
        "tt.account.followers",
        "follower_count",
        scope=TIKTOK_STATS,
        definition="Current number of followers of the TikTok account.",
        preferred_for="followers",
    ),
    _tt_account(
        "tt.account.following",
        "following_count",
        scope=TIKTOK_STATS,
        definition="Current number of accounts this TikTok account follows.",
    ),
    _tt_account(
        "tt.account.likes_total",
        "likes_count",
        scope=TIKTOK_STATS,
        definition="Total likes the TikTok account has received.",
    ),
    _tt_account(
        "tt.account.video_count",
        "video_count",
        scope=TIKTOK_STATS,
        definition="Number of videos on the TikTok account.",
    ),
    MetricSpec(
        key="tt.account.display_name",
        platform=Platform.TIKTOK,
        object_type=AnalyticsObjectType.ACCOUNT,
        provider_metric_name="display_name",
        provider_object="tiktok_user",
        source_endpoint="/v2/user/info/",
        source_doc_url=TT_DOC_USER,
        unit="text",
        value_kind=MetricValueKind.OBJECT,
        metric_class=MetricClass.POINT_IN_TIME,  # identity metadata kept as an object, never compared
        period_kinds=INSTANT,
        definition="The account's display name. Identity metadata, not a performance metric.",
        required_permissions=(TIKTOK_BASIC,),
    ),
    _tt_video("tt.video.views", "view_count", definition="Number of views of the video.", preferred_for="views"),
    _tt_video("tt.video.likes", "like_count", definition="Number of likes on the video."),
    _tt_video("tt.video.comments", "comment_count", definition="Number of comments on the video."),
    _tt_video("tt.video.shares", "share_count", definition="Number of shares of the video."),
    _tt_video(
        "tt.video.duration",
        "duration",
        unit="seconds",
        cls=MetricClass.POINT_IN_TIME,
        definition="Duration of the video in seconds. Object metadata, not performance.",
    ),
    _tt_video(
        "tt.video.created_at",
        "create_time",
        unit="epoch_s",
        cls=MetricClass.POINT_IN_TIME,
        definition="UTC epoch seconds when the video was posted. Used for attribution.",
    ),
)


#: Metrics the brief asks for that no approved API exposes. They exist so the UI can explain the
#: gap by name instead of leaving a blank tile, and so a future API can fill the row rather than
#: invent a new one.
NOT_SUPPORTED: tuple[MetricSpec, ...] = (
    MetricSpec(
        key="tt.video.watch_time",
        platform=Platform.TIKTOK,
        object_type=AnalyticsObjectType.VIDEO,
        provider_metric_name="watch_time",
        provider_object="tiktok_video",
        source_endpoint="/v2/video/list/",
        source_doc_url=TT_DOC_VIDEO,
        unit="ms",
        value_kind=MetricValueKind.SCALAR,
        metric_class=MetricClass.CUMULATIVE,
        period_kinds=LIFETIME,
        definition="Total watch time. Not exposed by any approved TikTok API.",
        required_permissions=(TIKTOK_VIDEO_LIST,),
        availability_default=MetricAvailability.NOT_SUPPORTED,
        preferred_for="watch_time",
    ),
    MetricSpec(
        key="tt.video.avg_watch_time",
        platform=Platform.TIKTOK,
        object_type=AnalyticsObjectType.VIDEO,
        provider_metric_name="average_watch_time",
        provider_object="tiktok_video",
        source_endpoint="/v2/video/list/",
        source_doc_url=TT_DOC_VIDEO,
        unit="ms",
        value_kind=MetricValueKind.SCALAR,
        metric_class=MetricClass.CUMULATIVE,
        period_kinds=LIFETIME,
        definition="Average watch time. Not exposed by any approved TikTok API.",
        required_permissions=(TIKTOK_VIDEO_LIST,),
        availability_default=MetricAvailability.NOT_SUPPORTED,
        preferred_for="avg_watch_time",
    ),
    MetricSpec(
        key="tt.video.retention_curve",
        platform=Platform.TIKTOK,
        object_type=AnalyticsObjectType.VIDEO,
        provider_metric_name="retention_graph",
        provider_object="tiktok_video",
        source_endpoint="/v2/video/list/",
        source_doc_url=TT_DOC_VIDEO,
        unit="ratio",
        value_kind=MetricValueKind.SERIES,
        metric_class=MetricClass.SERIES,
        period_kinds=LIFETIME,
        definition="Audience retention curve. Not exposed by any approved TikTok API.",
        required_permissions=(TIKTOK_VIDEO_LIST,),
        availability_default=MetricAvailability.NOT_SUPPORTED,
        preferred_for="retention",
    ),
    MetricSpec(
        key="tt.video.replays",
        platform=Platform.TIKTOK,
        object_type=AnalyticsObjectType.VIDEO,
        provider_metric_name="replay_count",
        provider_object="tiktok_video",
        source_endpoint="/v2/video/list/",
        source_doc_url=TT_DOC_VIDEO,
        unit="count",
        value_kind=MetricValueKind.SCALAR,
        metric_class=MetricClass.CUMULATIVE,
        period_kinds=LIFETIME,
        definition="Replays. Not exposed by any approved TikTok API.",
        required_permissions=(TIKTOK_VIDEO_LIST,),
        availability_default=MetricAvailability.NOT_SUPPORTED,
        preferred_for="replays",
    ),
    MetricSpec(
        key="tt.video.reach",
        platform=Platform.TIKTOK,
        object_type=AnalyticsObjectType.VIDEO,
        provider_metric_name="reach",
        provider_object="tiktok_video",
        source_endpoint="/v2/video/list/",
        source_doc_url=TT_DOC_VIDEO,
        unit="count",
        value_kind=MetricValueKind.SCALAR,
        metric_class=MetricClass.CUMULATIVE,
        period_kinds=LIFETIME,
        definition="Unique reach. Not exposed by any approved TikTok API.",
        required_permissions=(TIKTOK_VIDEO_LIST,),
        availability_default=MetricAvailability.NOT_SUPPORTED,
        preferred_for="reach",
    ),
    MetricSpec(
        key="tt.video.saves",
        platform=Platform.TIKTOK,
        object_type=AnalyticsObjectType.VIDEO,
        provider_metric_name="save_count",
        provider_object="tiktok_video",
        source_endpoint="/v2/video/list/",
        source_doc_url=TT_DOC_VIDEO,
        unit="count",
        value_kind=MetricValueKind.SCALAR,
        metric_class=MetricClass.CUMULATIVE,
        period_kinds=LIFETIME,
        definition="Saves. Not exposed by any approved TikTok API.",
        required_permissions=(TIKTOK_VIDEO_LIST,),
        availability_default=MetricAvailability.NOT_SUPPORTED,
        preferred_for="saves",
    ),
    MetricSpec(
        key="ig.media.reels_replays",
        platform=Platform.INSTAGRAM,
        object_type=AnalyticsObjectType.REEL,
        provider_metric_name="replays",
        provider_object="ig_media",
        source_endpoint="/{version}/{ig-media-id}/insights",
        source_doc_url=IG_DOC_MEDIA,
        unit="count",
        value_kind=MetricValueKind.SCALAR,
        metric_class=MetricClass.CUMULATIVE,
        period_kinds=LIFETIME,
        definition="Replays. Documented for Instagram as removed (plays) and not exposed per reel.",
        required_permissions=IG_PERMISSIONS,
        availability_default=MetricAvailability.NOT_SUPPORTED,
        preferred_for="replays",
    ),
)


METRIC_CATALOG: tuple[MetricSpec, ...] = (
    *FB_PAGE,
    *FB_POST,
    *FB_VIDEO,
    *IG_ACCOUNT,
    *IG_MEDIA,
    *TIKTOK,
    *NOT_SUPPORTED,
)


def catalogue_by_key() -> dict[str, MetricSpec]:
    return {spec.key: spec for spec in METRIC_CATALOG}


def metrics_for(platform: Platform, object_type: AnalyticsObjectType | None = None) -> list[MetricSpec]:
    return [
        spec
        for spec in METRIC_CATALOG
        if spec.platform is platform and (object_type is None or spec.object_type is object_type)
    ]


def preferred(platform: Platform, object_type: AnalyticsObjectType, purpose: str) -> MetricSpec | None:
    """The metric to show when the UI needs "reach" and the platform has three candidates."""
    candidates = [spec for spec in METRIC_CATALOG if spec.preferred_for == purpose]
    for spec in candidates:
        if spec.platform is platform and spec.object_type is object_type and not spec.is_deprecated:
            return spec
    for spec in candidates:
        if spec.platform is platform and spec.object_type is object_type:
            return spec
    return None


def ensure_catalog_is_consistent() -> None:
    """Fail at import/seed time, not at collection time.

    A metric without a class, a kind, a unit or a permission list cannot be compared, stored or
    planned correctly, so the catalogue refuses to exist in that state (same technique as
    ``agents/catalog.py::ensure_catalog_is_consistent``).
    """
    keys = [spec.key for spec in METRIC_CATALOG]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ValueError(f"duplicate metric keys in catalogue: {duplicates}")

    for spec in METRIC_CATALOG:
        if not spec.metric_class:
            raise ValueError(f"{spec.key} has no metric_class")
        if not spec.value_kind:
            raise ValueError(f"{spec.key} has no value_kind")
        if not spec.unit:
            raise ValueError(f"{spec.key} has no unit")
        if not spec.source_doc_url.startswith("https://"):
            raise ValueError(f"{spec.key} has no documentation URL")
        if not spec.period_kinds:
            raise ValueError(f"{spec.key} has no period kinds")
        if spec.key.split(".")[0] not in {"fb", "ig", "tt"}:
            raise ValueError(f"{spec.key} is not namespaced by platform")
        if spec.value_kind is MetricValueKind.SCALAR and spec.metric_class is MetricClass.SERIES:
            raise ValueError(f"{spec.key} is a scalar but declared as a series class")
        if spec.value_kind is MetricValueKind.SERIES and spec.metric_class is not MetricClass.SERIES:
            raise ValueError(f"{spec.key} is a series but its class is {spec.metric_class}")
        if spec.value_kind is MetricValueKind.BREAKDOWN and spec.metric_class not in (
            MetricClass.SERIES,
            MetricClass.CUMULATIVE,
        ):
            raise ValueError(f"{spec.key} is a breakdown but its class is {spec.metric_class}")
        if spec.metric_class is MetricClass.SERIES and spec.value_kind is MetricValueKind.SCALAR:
            raise ValueError(f"{spec.key} is declared a series class but stored as a scalar")
        if spec.is_deprecated and not spec.deprecated_note:
            raise ValueError(f"{spec.key} is deprecated without a note")
        if spec.is_deprecated:
            dated = spec.deprecation_scope == "all_versions"
            capped = spec.deprecation_scope == "api_version" and bool(spec.api_version_max)
            if not (dated or capped):
                raise ValueError(
                    f"{spec.key} is deprecated without deprecation_scope/api_version_max"
                )
        if isinstance(spec.required_permissions, dict):
            alternatives = spec.required_permissions.get("any_of") or []
            if not alternatives or not all(alternatives):
                raise ValueError(f"{spec.key} has a malformed required_permissions dict")
        is_tiktok_account = (
            spec.platform is Platform.TIKTOK and spec.object_type is AnalyticsObjectType.ACCOUNT
        )
        if is_tiktok_account and not {TIKTOK_STATS, TIKTOK_BASIC} & set(spec.required_permissions):
            raise ValueError(f"{spec.key} must declare user.info.stats or user.info.basic")
        is_tiktok_video = (
            spec.platform is Platform.TIKTOK and spec.object_type is AnalyticsObjectType.VIDEO
        )
        if is_tiktok_video and TIKTOK_VIDEO_LIST not in spec.required_permissions:
            raise ValueError(f"{spec.key} must declare video.list")


ensure_catalog_is_consistent()
