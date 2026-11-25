# Media delivery: the bucket Instagram fetches from

Facebook and TikTok accept an upload, so a video can go straight from the library to the provider.
Instagram does not: `POST /media` takes a `video_url` and Instagram fetches the bytes itself. That means
the file has to be reachable from the internet for a short while, which is a real decision with real
consequences, and this document is where that decision lives.

## What is required

| Setting | Meaning |
| --- | --- |
| `provider` | `s3`, `r2`, `b2`, `wasabi`, `minio` or `spaces` — anything that speaks S3 with a custom endpoint |
| `bucket`, `region` | where the object lands |
| `public_base_url` | only when the bucket is served through a CDN or a custom domain |
| `endpoint_url` | for non-AWS providers |
| `prefix` | object key prefix, `creatoros` by default |
| `credentials_secret_id` | an **id**, not a key: the value is sealed in the DPAPI vault by the Electron shell |
| `ttl_seconds` | signed-URL lifetime, 3600 by default (300–86400) |

Configure it on **Publishing → Media delivery**, or with `PUT /publishing/delivery`.

## Object keys are hashes, not filenames

`DeliveryService.object_key()` derives the key from the derivative's sha256 and profile, so:

* uploading the same derivative twice reuses one object instead of duplicating it;
* a filename can never influence where the object lands;
* the ledger's `object_key` is safe to show in the GUI.

## What happens during an Instagram publish

1. `preflight` refuses with `DELIVERY_NOT_CONFIGURED` if no bucket exists — before anything is uploaded.
2. The executor asks the delivery service to `ensure_uploaded()` the chosen derivative.
3. The provider is asked for the object's **size and `x-amz-meta-sha256`** after upload, so a truncated
   or corrupted transfer is caught here rather than at Instagram.
4. A signed URL with the configured TTL is created and handed to Instagram only.
5. The `publish_delivery` row records object key, size, sha256, upload time and URL expiry.
6. After publication, `release(object_key)` marks the row released; `cleanup()` deletes objects whose
   URL expired and increments `delete_attempts` on failures instead of silently retrying forever.

## What is deliberately absent

* **No localhost delivery.** There is an `UnconfiguredDeliveryProvider` and no local implementation, so
  "works on my machine" cannot be mistaken for configured.
* **No tunnel.** A public tunnel would expose the machine for every video and depend on an account
  nobody audited; it is not a supported configuration.
* **No cached provider instance.** A rotated credential or a changed bucket takes effect on the next
  call, which is the entire point of rotating it.

## Failure modes and what they look like

| Situation | What you see |
| --- | --- |
| no bucket configured | destination `blocked: DELIVERY_NOT_CONFIGURED`, Instagram only |
| wrong credentials | `POST /publishing/delivery/check` → `ok: false` with the provider's reason |
| bucket deleted under you | the upload is `retryable`; the destination settles with the category, not "published" |
| object expired before Instagram fetched it | transient failure → retry re-uploads a fresh object |
| cleanup cannot delete | row keeps `delete_attempts`, error detail is stored, nothing is pretended |

## Verifying without publishing

```
POST /publishing/delivery/check      # does the bucket answer with these credentials?
GET  /publishing/delivery            # configuration + object ledger
POST /publishing/delivery/cleanup    # release expired objects
```

Fixtures for the whole path live in `tests/test_publishing_delivery.py` (27 tests) and use a stubbed
S3 endpoint; no live bucket was contacted on this machine (see `docs/evidence/P7.md`, category D).
