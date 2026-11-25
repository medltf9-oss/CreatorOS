# TikTok integration

TikTok Login Kit, PKCE mandatory, rotating refresh tokens. ADR-12.

| | |
| --- | --- |
| Route | `tiktok_login` |
| Authorize | `https://www.tiktok.com/v2/auth/authorize/` |
| Token | `https://open.tiktokapis.com/v2/oauth/token/` |
| API | `https://open.tiktokapis.com/v2` |
| Scopes | `user.info.basic`, `video.list`, `video.upload` (+ opt-in `video.publish`, `user.info.profile`, `user.info.stats`) |
| PKCE | **required**, S256 |
| Access token lifetime | 24 hours |
| Refresh token lifetime | 365 days, **rotates on every refresh** |
| Revoke | `POST /v2/oauth/revoke/` |
| Docs | https://developers.tiktok.com/doc/oauth-user-access-token-management |

## PKCE

Desktop apps must use PKCE; TikTok rejects an authorization request without `code_challenge`.
The engine generates a 96-character verifier (`secrets.token_urlsafe(64)[:96]`), stores it in the
vault with purpose `oauth_pkce_verifier`, and sends only the S256 challenge:

```
code_challenge_method=S256
code_challenge=base64url(sha256(verifier))   # no padding
```

`TikTokAdapter.authorization_url` raises `PKCE_REQUIRED` if no challenge is supplied, and
`exchange_code` raises `PKCE_REQUIRED` if the verifier is missing. There is no silent downgrade to a
challenge-less flow.

## Token exchange

`POST /v2/oauth/token/` (form-encoded) with `client_key`, `client_secret`, `code`,
`grant_type=authorization_code`, `redirect_uri`, `code_verifier` returns:

```json
{"access_token": "…", "expires_in": 86400, "refresh_token": "…", "refresh_expires_in": 31536000,
 "scope": "user.info.basic,video.list", "token_type": "Bearer", "open_id": "…"}
```

Two details matter:

* `open_id` identifies the TikTok user — it is the `provider_account_id`.
* `scope` is a **comma-separated string**, not a list. It is split and stored; the provider returns
  it, so `scope_source = "token_response"` (falling back to `consent_request` when absent).

## Rotating refresh tokens

Every refresh invalidates the previous refresh token. A refresh that succeeds at TikTok but fails
before the new token is stored therefore **destroys** the account's ability to refresh. CreatorOS
treats a refresh as a transaction (`services/social/tokens.py`, `TOKEN_LIFECYCLE.md`):

1. `credential_rotation` row, `pending`, with a lock token;
2. conditional `UPDATE … WHERE state='pending'` → `in_progress` (one winner);
3. an encrypted journal secret (`rotation_journal`) naming the old reference and the intent;
4. the provider call;
5. new tokens stored, account repointed, rotation `completed`;
6. on failure, `failed` + provider code, and a decision about whether the account is still usable.

A rotation left `in_progress` by a crash is resolved by `resolve_interrupted`: if a usable
credential is still stored the rotation is `abandoned`, otherwise the account is marked
`reauthorization_required`. Guessing is not an option, and the vault is never left holding an
unknown state.

Because access tokens live 24 hours, "expiring soon" is 6 hours (`social_token_expiry_warn_hours`);
a 24-hour window would mark every healthy TikTok account as expiring.

## Capabilities

| Capability | Required scope | Notes |
| --- | --- | --- |
| `tiktok.read_profile` | `user.info.basic` | available after connect |
| `tiktok.list_videos` | `video.list` | available after connect |
| `tiktok.upload_draft` | `video.upload` | available after connect; uploads land in the creator's inbox as a draft |
| `tiktok.direct_post` | `video.publish` | requires the Direct Post add-on on the developer app; login succeeding never implies it |
| `manual.export_package` | none | `manual_only`, always available: a human publishes by hand |

`video.publish` is never requested implicitly. It is an extra scope the user can opt into
(`ROUTE_SPECS[...].extra_scopes`), and even then `tiktok.direct_post` stays `needs_user_consent`
until the granted scope list contains it.

## The one POST that is not a publish

`POST /v2/post/publish/creator_info/query/` is a **read-only** metadata call used by diagnostics.
It answers "can this creator use Direct Post, what privacy levels exist, what are the limits" and
creates nothing. It is the only POST in the adapter, and `test_social_tiktok.py` asserts that no
adapter code path contains a publish, upload or post endpoint.

## Error mapping

`TIKTOK_ERROR_MAP`:

| TikTok code | CreatorOS code |
| --- | --- |
| `access_token_invalid`, `access_token_expired` | `TOKEN_EXPIRED` |
| `refresh_token_invalid`, `refresh_token_expired`, `invalid_grant` | `REAUTHORIZATION_REQUIRED` |
| `scope_not_authorized`, `scope_permission_missed` | `SCOPE_NOT_GRANTED` |
| `rate_limit_exceeded` | `RATE_LIMITED` |
| `internal_error` | `PROVIDER_UNAVAILABLE` |
| `invalid_client` | `APP_NOT_CONFIGURED` |
| `invalid_request` | `PROVIDER_ERROR` |

TikTok returns `HTTP 200` with `{"error": {"code": "ok"}}` on success and a non-`ok` code on
failure, so the adapter treats the **code** as the verdict, not the status line. A body with
`error.code == "ok"` is a success even though the field is called "error"; anything else is mapped
through the table above.

## Limits

* No publish/upload is implemented. `tiktok.upload_draft` and `tiktok.direct_post` describe
  permissions; Phase 7 implements the calls.
* Direct Post requires TikTok's approval of the add-on; the capability is reported as such.
* Access tokens live 24 h, so a long-running job must refresh or the user reconnects.
* Revocation uses `/v2/oauth/revoke/` and its answer is reported verbatim.
