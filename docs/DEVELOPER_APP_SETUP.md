# Developer app setup

CreatorOS cannot connect anything until you have created an app in each platform's developer
console. This is what to create, what to copy, and what each console will ask you for.

Create **one app per platform** (Meta's one app covers both Instagram paths). Everything stays on
this machine: no client secret is stored anywhere except the DPAPI vault.

---

## 1. Meta app (Facebook Pages + both Instagram paths)

1. Go to <https://developers.facebook.com/apps> → **Create app** → use case **Other** →
   type **Business**.
2. Note the **App ID** and **App Secret** (Settings → Basic). The App ID is your client id.
3. Add the product **Facebook Login** (for Facebook Pages and Instagram-via-Facebook-Login) and
   **Instagram** → **API setup with Instagram login** (for the Instagram Login path) if you want both.
4. In **Facebook Login → Settings**, add a **Valid OAuth Redirect URI**:

   ```
   http://127.0.0.1:47821/oauth/callback/facebook_login
   ```

   If you also use Instagram via Facebook Login, the same URI serves it (it is the same route).
5. For the Instagram Login path, add its own redirect:

   ```
   http://127.0.0.1:47821/oauth/callback/instagram_login
   ```

   (Instagram Login is configured under **Instagram → API setup with Instagram login → Business
   login settings**.)
6. Request these permissions in the app dashboard:

   | Path | Scopes |
   | --- | --- |
   | Facebook Pages | `pages_show_list`, `pages_read_engagement`, `pages_manage_posts` |
   | Instagram Login | `instagram_business_basic`, `instagram_business_content_publish` |
   | Instagram via Facebook Login | `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement` |

   `read_insights` / `instagram_*_manage_insights` / `user.info.stats` are **not** requested by the
   connect flow; they need app review and CreatorOS reports them as `requires_developer_approval` until
   you add them deliberately. Once the app is approved, a user grants them per account through
   *Analytics → Account performance* or *Social accounts → Permissions → Grant analytics access*
   (P8.20); the app just has to declare the permission.
7. While the app is in **Development** mode only app testers (Roles → Testers) can authorize. That is
   fine for a first run: CreatorOS will show app mode `development` everywhere it matters.
8. Copy the App ID into CreatorOS → *App settings* → **Facebook Login (Pages)** → **Client ID**, and
   the App Secret into **Client secret** → **Save app**. Repeat for *Instagram Login* and *Instagram
   via Facebook Login* using the same App ID/secret.

Graph version: CreatorOS defaults to **v23.0**. If your app is pinned to another version, set it in
the app record's **API version**.

---

## 2. TikTok app

1. Go to <https://developers.tiktok.com/> → **Manage apps** → **Connect an app**.
2. Add the products **Login Kit** and (optionally) **Content Posting API**.
3. **Login Kit → Redirect URI** must be exactly:

   ```
   http://127.0.0.1:47821/oauth/callback/tiktok_login
   ```

   TikTok requires **https** for web redirects but accepts an `http` loopback address for desktop
   apps; if your console refuses it, TikTok's desktop flow is the one that allows `127.0.0.1`.
4. Scopes to request: `user.info.basic`, `video.list`, `video.upload`. Add `video.publish` **only**
   if you have the Direct Post add-on approved — CreatorOS never asks for it implicitly.
5. Copy the **Client key** into CreatorOS → *App settings* → **TikTok Login Kit** → **Client ID**
   (TikTok calls it `client_key`; CreatorOS sends the stored id under that name) and the **Client
   secret** into **Client secret** → **Save app**.
6. PKCE is mandatory for this route; CreatorOS generates the verifier, seals it in the vault and
   sends only the S256 challenge. Nothing to configure.

---

## What CreatorOS verifies for you

*App settings → Verify configuration* checks, locally and honestly:

| Check | What it means |
| --- | --- |
| `redirect_uri` | loopback, `127.0.0.1`, correct path, no query |
| `scopes` | every scope the route needs is registered on the app record |
| `client_secret` | a secret is sealed in the vault and its `secret_ref` is `active` |
| `app_mode` | development vs. live, with the consequence stated |
| `provider_reachability` | **`unknown`** — never claimed. Verifying the app at the provider needs a user authorization, which only a human can give |

*Account details → Run diagnostics* is the only place a provider is actually contacted with a token,
and it prints a redacted report.

---

## Checklist

- [ ] Meta app created, App ID + App Secret copied into all three Meta routes you intend to use
- [ ] `http://127.0.0.1:47821/oauth/callback/facebook_login` and
      `http://127.0.0.1:47821/oauth/callback/instagram_login` registered in the console
- [ ] TikTok app created, `http://127.0.0.1:47821/oauth/callback/tiktok_login` registered
- [ ] Scopes requested (and only the ones you actually want granted)
- [ ] Each app shows **secret stored** in CreatorOS
- [ ] *Verify configuration* reports no problems
- [ ] Connect a test account, then check the capability matrix for what it can actually do

## Common console mistakes

| Symptom | Cause |
| --- | --- |
| `URL blocked: This redirect failed because the redirect URI is not whitelisted` | the console URI differs (https, `localhost`, trailing slash) |
| `APP_NOT_CONFIGURED` on connect | client id saved but secret not sealed |
| `invalid_client` from TikTok | client key/secret swapped, or the wrong app selected |
| every capability `requires_developer_approval` | app in development mode, or app review not granted |
| Instagram account not discovered | the professional account is not linked to a Page (use Instagram Login instead) |
