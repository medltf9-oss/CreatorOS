# Account capabilities

A capability is one thing an account may or may not be able to do. CreatorOS records a **verdict**
for each one, derived from stored facts, with a reason in words. ADR-24.

## The catalogue

Fourteen capabilities, one table (`integrations/social/config.py`, `CAPABILITY_CATALOGUE`):

| Key | Label | Platform | Required scope(s) | Headline |
| --- | --- | --- | --- | --- |
| `facebook.page.read_profile` | Read Page profile | Facebook | `pages_read_engagement` | yes |
| `facebook.page.publish_text` | Publish text posts to a Page | Facebook | `pages_manage_posts` | yes |
| `facebook.page.publish_video` | Publish video to a Page | Facebook | `pages_manage_posts`, `pages_read_engagement` | yes |
| `facebook.page.read_insights` | Read Page insights | Facebook | `read_insights`, `pages_read_engagement` (+ Page `ANALYZE` task) | **no** |
| `instagram.read_profile` | Read Instagram profile | Instagram | A: `instagram_business_basic` · B: `instagram_basic` | yes |
| `instagram.publish_reels` | Publish Reels | Instagram | A: `instagram_business_content_publish` · B: `instagram_content_publish` | yes |
| `instagram.publish_image` | Publish images | Instagram | A: `instagram_business_content_publish` · B: `instagram_content_publish` | yes |
| `instagram.read_insights` | Read Instagram insights | Instagram | A: `instagram_business_manage_insights` · B: `instagram_manage_insights` | **no** |
| `tiktok.read_profile` | Read TikTok profile | TikTok | `user.info.basic` | yes |
| `tiktok.list_videos` | List own TikTok videos | TikTok | `video.list` | yes |
| `tiktok.upload_draft` | Upload to TikTok inbox (draft) | TikTok | `video.upload` | yes |
| `tiktok.direct_post` | TikTok Direct Post | TikTok | `video.publish` | yes |
| `tiktok.read_insights` | Read TikTok account statistics | TikTok | `user.info.stats` | **no** |
| `manual.export_package` | Manual export package | TikTok | — (`manual_only`) | yes |

`headline=false` means: this capability needs a permission the connect flow deliberately does not
request, so it *cannot* be available yet. It still appears — with its reason — but it does not drag
the account's headline state down. Facebook/Instagram/TikTok insights are the cases; the user grants
them through the consent path (`P8.20`, `docs/OAUTH_ENGINE.md` § permission upgrade).

`route_scopes` exists because Instagram has two scope vocabularies. A capability reachable through
both paths must be judged against the scopes of *the path the account used*; otherwise every
capability looks missing on one of them. `instagram.read_insights` uses it: Instagram Login asks for
`instagram_business_manage_insights`, Facebook Login asks for `instagram_manage_insights`, and neither
satisfies the other.

`required_page_tasks` exists because a Facebook Page role is not an OAuth grant. Holding
`read_insights` on the token is not the same as being allowed to read that Page's insights; the user
must also hold the Page `ANALYZE` task, reported by the provider at discovery and stored on the
account. Both conditions must be true for `facebook.page.read_insights` to read `available`.

## The nine canonical states

| State | Meaning | Typical cause |
| --- | --- | --- |
| `available` | every required scope is granted and the token works | connected account, app live |
| `needs_user_consent` | the account did not grant a required scope | user unticked a permission |
| `requires_developer_approval` | the permission exists but the app is in development mode / not approved | app mode `development` |
| `unsupported` | this account type or login path cannot reach it | personal profile, wrong route, a capability no login path exposes |
| `unavailable` | temporarily unusable | provider incident, rate limit, lost credential |
| `unknown` | no evidence recorded | account just created, evaluation never ran |
| `expired` | blocked by an expired token | see `TOKEN_LIFECYCLE.md` |
| `revoked` | blocked by a revoked token | disconnected or provider revoked |
| `manual_only` | always done by a human | `manual.export_package` |

`CAPABILITY_STATES` also keeps legacy members (`eligible`, `missing_permission`, `needs_app_review`,
`token_expired`, `rate_limited`) so old rows can be read. `normalize_capability_state` maps them onto
the canonical nine at the boundary, which is why two vocabularies can never reach the GUI.

## Evaluation order

`capability_rules.evaluate` checks, in order, and returns the **first** matching verdict:

1. **route unreachable** → `unsupported` ("only reachable through …, this account uses …")
2. **manual only** → `manual_only`
3. **wrong account type** → `unsupported` ("needs a facebook_page, this one is …")
4. **dead token** → `expired` / `revoked` / `unavailable`
5. **all scopes granted, Page task missing or never reported** → `needs_user_consent` (only for
   capabilities with `required_page_tasks`)
6. **all scopes granted** → `available`
7. **missing scope + app not approved** → `requires_developer_approval`
8. **missing scope + app in development** → `requires_developer_approval` ("only granted to app testers")
9. **missing scope otherwise** → `needs_user_consent`

Every verdict carries `evidence`: app mode, route, account type, token state, granted scopes and
`evaluated_from: "stored account facts"`. That is what makes "why is this disabled?" answerable
without guessing.

`overall_state` collapses a set to one headline: the worst of `revoked` > `expired` > `unavailable` >
`requires_developer_approval` > `needs_user_consent` > `unsupported`, then `manual_only`, then
`available`.

## Where verdicts live

* `account_capability` — current verdict per (account, capability), unique on the pair, with
  `reason`, `required_scopes`, `granted_scopes`, `missing_scopes`, `verified_at`, `api_version`.
* `capability_snapshot` — immutable history: each evaluation writes one row with its `trigger`
  (`oauth_callback`, `manual_refresh`, `token_change`, `verify`) and the granted scope set.

`connection_status` on the account is **credential health** (does the token work), not the
permission set. A connected account with a missing permission is `connected` with a
`needs_user_consent` capability — conflating the two is how a UI ends up saying "not connected"
about an account that is connected.

## API

```
GET  /social/accounts/{id}/capabilities           → verdicts + snapshot history
POST /social/accounts/{id}/capabilities/refresh   → re-evaluate from stored facts (no network)
GET  /social/capabilities/matrix                  → capabilities × accounts, totals, catalogue
GET  /analytics/consent/{account_id}              → what insights is missing, and who can fix it
POST /analytics/consent/{account_id}              → begin the permission upgrade (P8.20)
```

`refresh` never contacts the provider: it recomputes from the scopes already stored, including the
Page task list last reported by the provider. A provider round trip happens on
`POST /social/accounts/{id}/verify` and on the consent callback, which are the only paths that call
out. The consent path is the one place a capability that needs an extra permission can move from
`needs_user_consent`/`requires_developer_approval` to `available` — and only when the provider
actually grants it.

## The matrix

`GET /social/capabilities/matrix` returns:

```json
{"capabilities": ["facebook.page.read_profile", "…"],
 "accounts": [{"id": "…", "platform": "facebook", "label": "…", "route": "facebook_login",
               "states": {"facebook.page.read_profile": "available", "…": "needs_user_consent"}}],
 "totals": {"available": 3, "needs_user_consent": 4, "requires_developer_approval": 2},
 "catalogue": [{"key": "…", "label": "…", "platform": "…", "routes": ["…"],
                "required_scopes": ["…"], "manual_only": false, "docs_url": "…", "notes": "…"}]}
```

The matrix is the source for the Permissions page. Rows are never dropped: a capability with no
record for an account renders as an em dash, which is visibly different from `unsupported`.

## Verified by tests

`tests/test_social_capabilities.py` (18): every state is reachable and its reason mentions the right
thing; legacy strings normalize; `headline=false` capabilities do not change the account state;
route-filtering does not hide reachable-by-another-route capabilities; Instagram's two vocabularies
resolve per route; snapshots are append-only.
