# Facebook integration

Facebook Login for **Pages** only. A personal profile cannot be published to through the Graph API,
so it is discovered, reported as `unsupported_account_type`, and skipped. ADR-10.

| | |
| --- | --- |
| Route | `facebook_login` |
| Authorize | `https://www.facebook.com/v23.0/dialog/oauth` |
| Token | `https://graph.facebook.com/v23.0/oauth/access_token` |
| Graph | `https://graph.facebook.com/v23.0` |
| Scopes | `pages_show_list`, `pages_read_engagement`, `pages_manage_posts` |
| PKCE | not used by this flow |
| Refresh token | none — long-lived extension instead |
| Revoke | `DELETE /me/permissions` |
| Docs | https://developers.facebook.com/docs/facebook-login/guides/advanced/manual-flow |

## Code exchange

1. `GET /oauth/access_token` with `client_id`, `client_secret`, `redirect_uri`, `code` → short-lived
   user token (~1–2 h).
2. Immediately extend it: `GET /oauth/access_token?grant_type=fb_exchange_token` → long-lived user
   token (~60 days). If the extension fails the short-lived token is still stored, and
   `bundle.meta.long_lived_error` records why — a silently unextended token would look healthy for
   two hours and then die.
3. `GET /me/accounts` (paged, `limit=100`, max 10 pages) → the Pages this user administers. Each
   Page is a separate account with **its own** access token; the Page token minted from a long-lived
   user token does not expire (`expires_at=None`), which is recorded as a fact rather than assumed.
4. `debug_token` on the user token (`input_token` + `access_token` = app token) supplies the granted
   scope list and the app id. This is the only path that needs the client secret in memory.

`appsecret_proof = HMAC-SHA256(access_token, client_secret)` is sent on `/me/accounts` and
`debug_token` whenever the secret is available; `base.py` computes it once so no adapter can
accidentally send a token without it.

## What is stored per Page

```
provider_account_id  Page id
display_name         Page name
username             Page username if present
avatar_url           Page picture
account_type         facebook_page
scopes               granted scopes reported by debug_token (scope_source = "debug_token")
meta_json            category, can_post (task CREATE_CONTENT present), instagram_business_account id
```

`can_post` is derived from the `tasks` array, not from a successful publish. It is evidence, not a
promise.

## Capabilities

| Capability | Required scope(s) | Notes |
| --- | --- | --- |
| `facebook.page.read_profile` | `pages_read_engagement` | available after connect |
| `facebook.page.publish_text` | `pages_manage_posts` | available after connect, in development mode only once the app is live |
| `facebook.page.publish_video` | `pages_manage_posts` + `pages_read_engagement` | available after connect |
| `facebook.page.read_insights` | `read_insights` + `pages_read_engagement` | `headline=false`: `read_insights` is **not** requested at connect time (Meta requires app review), so it appears as `requires_developer_approval` instead of dragging the headline state down |

## Error mapping

`FACEBOOK_ERROR_MAP` in `integrations/social/errors.py`:

| Graph code | CreatorOS code |
| --- | --- |
| 4, 17, 32, 613 | `RATE_LIMITED` |
| 10, 200 | `SCOPE_NOT_GRANTED` |
| 190, 102, 463, 467, 460 | `TOKEN_EXPIRED` |
| 458, 459 | `APP_REVIEW_REQUIRED` |
| 2, 1 | `PROVIDER_UNAVAILABLE` |
| anything else | `PROVIDER_ERROR` with the raw code in `detail` |

## Disconnecting

`DELETE /me/permissions` removes the app's permissions for that user. CreatorOS reports
`revoked_at_provider: true` only when Graph answers `{"success": true}`; otherwise the local
credential is still deleted and the detail string says what the provider did.

## Limits that are stated, not worked around

* Personal profiles: not connectable. Shown as unsupported.
* Page enumeration is capped at 10 pages (100 each) so a pathological account cannot stall the
  callback; `social_max_accounts_per_claim` (100) is enforced again at claim time.
* `read_insights` needs app review. It is reported as a developer-approval requirement, never faked,
  and is granted per account through the consent path (P8.20) once the app is approved. The Page
  `ANALYZE` task is a Page role, checked separately from the OAuth grant.
* App mode `development` is shown everywhere it matters: only app testers can authorize, and most
  publish permissions need review before real users can use them.
