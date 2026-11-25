# ADR-28 — Instagram's media URL comes from a configured cloud bucket, or Instagram is blocked

**Status:** Accepted, implemented and tested (Phase 7)
**Date:** 2026-11-24

## Context

Instagram's Content Publishing API does not accept an upload for Reels: it takes a `video_url` and
fetches the bytes itself. Three shortcuts are tempting and all of them are wrong here:

1. **A local HTTP server on a tunnel** (ngrok, cloudflared, a public IP). It publishes the user's
   machine to the internet for every video, depends on a third-party account nobody audited, and breaks
   the moment the tunnel URL changes. It also makes "the media left the machine" invisible.
2. **A localhost URL.** Instagram cannot reach it. The only way this "works" in a test is a fake
   provider, which is exactly the kind of simulation this project bans.
3. **Pretending Instagram works and failing at upload time.** Users find out after they approved.

## Decision

1. **A first-class `MediaDeliveryProvider`**, with an S3-compatible implementation (`boto`-free, own
   SigV4: `s3`, `r2`, `b2`, `wasabi`, `minio`, `spaces`) and an **`UnconfiguredDeliveryProvider`** that
   is a real state, not a test stub.

2. **Instagram is `blocked: delivery_not_configured` until a bucket exists**, in the destination's
   preflight, in `/version` (`publish_instagram: no_delivery_bucket`) and in the GUI. There is
   deliberately **no localhost delivery implementation in the codebase**.

3. **Credentials live in the DPAPI vault**, written by the Electron shell; the API receives a
   `credentials_secret_id` and can never read the value. The bucket name, region, prefix and URL
   lifetime are ordinary settings.

4. **Every delivery is a row** in `publish_delivery`: object key (derived from the derivative hash,
   never a filename), size, sha256, upload time, URL expiry, deletion time and attempts. The ledger
   answers "what did we hand to the internet, when, and is it gone".

5. **Objects are verified, not assumed.** After upload the provider is asked for the object's size and
   its `x-amz-meta-sha256`, so a truncated or corrupted upload is caught before the provider fetches it.

6. **Signed URLs are secrets.** `SignedUrl` suppresses its URL in `repr`, the executor passes one to
   Instagram and nowhere else, and `PublishHistory` strips `url`/`signed_url`/`upload_url` keys from
   anything it projects.

7. **The delivery provider is not cached** — a rotated credential or changed bucket takes effect on the
   next call, which is the whole point of rotating one.

## Consequences

* Instagram publishing cannot run on this machine until a bucket is configured. That is reported
  everywhere instead of being hidden, and it is why no live Instagram test exists (`docs/evidence/P7.md`,
  category D).
* Facebook and TikTok do not need this path: they accept chunked uploads, and the GUI says so.
* Retention cleanup (`POST /publishing/delivery/cleanup`) deletes expired objects and keeps the ledger
  rows, so the audit trail survives the object.
* A user who never posts to Instagram never configures anything, and nothing leaves the machine.
