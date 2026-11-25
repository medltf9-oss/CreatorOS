# Instagram integration

Instagram has **two** official login paths that produce different ids, different scopes and
different capabilities. CreatorOS implements both and never merges them (ADR-11).

| | Path A — Instagram Login | Path B — via Facebook Login |
| --- | --- | --- |
| Route | `instagram_login` | `facebook_login` (the Meta app's Facebook Login) |
| Authorize | `https://www.instagram.com/oauth/authorize` | `https://www.facebook.com/v23.0/dialog/oauth` |
| Token | `https://api.instagram.com/oauth/access_token` | `https://graph.facebook.com/v23.0/oauth/access_token` |
| Graph | `https://graph.instagram.com/v23.0` | `https://graph.facebook.com/v23.0` |
| Scopes | `instagram_business_basic`, `instagram_business_content_publish` (+ optional `instagram_business_manage_insights`) | `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement` |
| Needs a Page | no | **yes** — discovery walks the user's Pages and reads `instagram_business_account` |
| PKCE | no | no |
| Refresh | `GET /refresh_access_token` (long-lived token extension) | Page token from `/me/accounts` |
| Revoke | **no documented endpoint** | `DELETE /me/permissions` on the Facebook app |
| Docs | [Instagram API with Instagram Login](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login) | [IG User reference](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user) |

## Why they are never merged

The same human can reach the same Instagram professional account through both paths. The two
paths produce **different** provider account ids (Instagram-scoped id vs. the Page-linked business
account id), different token types and different scope vocabularies. Merging on `@username` would be
a guess: usernames change, two businesses can share a display name, and a wrong merge would attach a
credential to the wrong account — the worst possible failure for a publishing tool.

So each path claims its own `social_account` row. When discovery finds an account that already
exists for the same route it is recorded as a **duplicate observation**; when a *different* route
already holds the same provider id, that is recorded too, and the account detail page states which
path owns which row.

## Path A — Instagram Login

1. `POST https://api.instagram.com/oauth/access_token` with `client_id`, `client_secret`,
   `grant_type=authorization_code`, `redirect_uri`, `code` → short-lived token (1 h) plus
   `user_id` and, when the provider returns it, a `permissions` array.
2. `GET https://graph.instagram.com/access_token?grant_type=ig_exchange_token&client_secret=…&access_token=…`
   → long-lived token (~60 days). If this fails, the short-lived token is stored and
   `meta.long_lived_error` records the reason.
3. `GET /me?fields=user_id,username,name,account_type,profile_picture_url,followers_count,media_count`
   → the professional account.
4. Refresh: `GET /refresh_access_token?grant_type=ig_refresh_token&access_token=…` — the token is
   extended, not replaced, so there is no rotating refresh token and no rotation journal entry.

Scope reporting: Instagram Login does not always echo `permissions`. When it does,
`scope_source = "token_response"`. When it does not, `scope_source = "consent_request"` and the
requested scopes are recorded as the evidence — and the GUI says exactly that, because "granted"
and "requested" are not the same claim.

## Path B — Instagram via Facebook Login

The Facebook adapter does the OAuth work (`FACEBOOK_INTEGRATION.md`); the Instagram adapter reads
`instagram_business_account.id` from each Page in `/me/accounts` and claims it as
`instagram_business`. The linked Page id and name are kept in `meta_json`, and the credentials are
the Page token (which, from a long-lived user token, does not expire).

## Capabilities

| Capability | Path A scope | Path B scope |
| --- | --- | --- |
| `instagram.read_profile` | `instagram_business_basic` | `instagram_basic` |
| `instagram.publish_reels` | `instagram_business_content_publish` | `instagram_content_publish` |
| `instagram.publish_image` | `instagram_business_content_publish` | `instagram_content_publish` |
| `instagram.read_insights` | `instagram_business_manage_insights`, `headline=false` | `instagram_manage_insights`, `headline=false` |

The catalogue handles the two vocabularies with `CapabilityDefinition.route_scopes` and
`scopes_for(route)`. Without it, an account connected by one path would be judged against the
other path's scope names and every capability would look missing.

Both insight scopes are opt-in (P8.20): the connect flow never requests them, and a user grants them
per account through the consent path (`docs/OAUTH_ENGINE.md` § permission upgrade). A path that
genuinely cannot reach a capability still lists it as `unsupported` with a reason; hiding it would make
the permissions matrix lie by omission.

## Revocation, honestly

Instagram Business Login documents **no** token revocation endpoint. `InstagramAdapter.revoke` on
path A returns:

> Instagram Business Login documents no token revocation endpoint. The credential was removed from
> this machine; remove CreatorOS from Instagram > Apps and websites to invalidate it at the
> provider.

`revoked_at_provider` is `false` in that case, and the disconnect result says so. Inventing a
"revoked" state would be the easiest lie in this phase.

## Known limits

* Path A tokens cannot be revoked from CreatorOS; the user must remove the app in Instagram.
* The long-lived exchange can fail independently of the code exchange; the token is still usable for
  an hour and `meta.long_lived` records the failure.
* Path B requires a Page, so a professional account with no Page is only reachable through path A.
* Insights require a permission the connect flow does not request, on both paths.
* Nothing here publishes. `instagram.publish_*` describes a permission, not an action: the publish
  endpoints arrive in Phase 7.
