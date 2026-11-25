# Publishing safety: the security review for Phase 7

Phase 7 is the first part of CreatorOS that sends bytes to a third party. This is the review that was
done with it, the rules that came out of it, and the tests that hold them.

`tests/test_publishing_security.py` (30 tests) is the executable form of this document.

## 1. The approval gate

* Every destination needs a consent record before its first provider call. The record is bound to
  `sha256(canonical payload)`, where the payload is derived from the rows the executor will actually
  read (caption, hashtags, options, account, media, schedule, privacy setting, validation outcome).
* `PublishApprovalService.verify()` re-derives the payload and fails closed on any difference. A
  12-case tamper matrix is in `test_publishing_approvals.py` (caption, hashtag, account, media,
  schedule, option, privacy, validation, plan, mode, action, consent version).
* The approval is **consumed before the first provider call**, in the same transaction that starts the
  attempt. A consumed approval cannot be reused; a retry therefore needs a fresh one (and the
  destination goes back to `draft` so it can be granted).
* No agent can request, decide or consume an approval. The three agent tools are read-only plus
  draft creation (`docs/PUBLISHING_AGENT_TOOLS.md`).

## 2. SSRF: two URLs the application does not fully control

Instagram is handed a `video_url`; TikTok hands back an `upload_url`. Both are checked by
`validate_provider_url()`:

* `https` only, default port only, no userinfo;
* a hostname, never an IP literal;
* `localhost`, `metadata.google.internal` and every private/loopback/link-local/reserved address are
  refused by name *and* by address class;
* the host must be one of the provider's own (`graph.facebook.com`, `graph.instagram.com`,
  `open.tiktokapis.com`, `*.tiktokcdn.com`, …) or a host the user configured for their bucket;
* redirects are never followed, so a passed check cannot be bounced somewhere private afterwards.

TikTok's upload URL is additionally sealed in the vault (next section) rather than stored anywhere the
journal, a log or an error message could reach.

## 3. The upload URL is a secret

`upload_url` is, in practice, a bearer token in a query string. Therefore:

* it is never written to `publish_job` (only the `publish_id` is), never to `publish_attempt`, never to
  the queue payload and never to an audit row;
* `PublishContext.upload_url` is declared `repr=False`, so printing the context cannot print it;
* the executor seals it in the vault under purpose `provider_upload_session` the moment it arrives, and
  unseals it only for the resume call;
* `SignedUrl` (the media side) suppresses its URL in `repr` for the same reason.

## 4. Redaction of everything leaving the backend

`PublishHistory.redact()` strips, at any depth: `access_token`, `token`, `secret`, `client_secret`,
`upload_url`, `signed_url`, `url`, `presigned`, `authorization`, `password`, `refresh_token`,
`code_verifier`. It is applied to every JSON column the API projects (`options`, `metadata`,
`field_provenance`, attempt `remote_snapshot`, reconciliation `evidence`).

The list is one constant (`REDACTED_KEYS`) so it cannot drift between routes, and the API test scans
whole response bodies for the session token, an access token and an upload URL.

## 5. What the API cannot even express

The request schemas have no field named `token`, `access_token`, `secret`, `secret_key`,
`secret_access_key`, `access_key_id`, `password`, `upload_url`, `signed_url` — and none named
`media_path`, `source_path`, `output_path`, `path`, `file`, `filename`. A test asserts both sets against
every model in `creatoros/schemas/publishing.py`, so a future field cannot slip in unnoticed.

Delivery credentials are a **vault secret id** produced by the Electron shell (ADR-22/ADR-28). The
backend verifies the reference is an active `delivery_credentials` secret and never sees the value.

## 6. One module may call a provider

`test_the_executor_is_the_only_module_that_calls_a_publisher` scans `services/` for publisher imports;
only `executor.py` and `reconciliation.py` (which only reads) may appear. `toolbox.py` is separately
asserted to contain no `PublishExecutor`, no `PublishActions`, no `.enqueue(` and no `publish_now`.

## 7. Logging

A blunt scan of `services/publishing` and `integrations/social/publishing` forbids `print(`, f-string
log calls, and any `log.<level>(...)` that mentions `token`, `upload_url` or `signed_url` on the same
line. An `Authorization: Bearer {token}` header is fine; logging it is not.

## 8. Properties this review does *not* claim

* No protection against a compromised local machine: the vault is DPAPI under the user's account, which
  is exactly as strong as that account.
* No signature verification of provider responses beyond TLS.
* No rate-limit shaping beyond what each publisher already reports (`429` → retryable category).
* No exactly-once publishing: the guarantees are no accidental duplicate within an intent, no blind
  resubmission after an ambiguity, and reconciliation before any retry.
