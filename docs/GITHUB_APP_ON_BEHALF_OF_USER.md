# Making a GitHub App Post on Behalf of a User

**Research date:** 2026-08-04
**Status:** Research / design reference (no code changes yet)

## TL;DR

A GitHub App **can** post comments, issues, PRs, and reviews that are **attributed to a specific user** (not the bot). This is done via **user-to-server (U2S) requests** using a **user access token** (`ghu_...`), obtained through GitHub's OAuth flow. The user must **authorize** the app first.

This is distinct from the current codebase behavior, which uses **installation tokens** (server-to-server) and posts as the bot (`hannibal-hub-agents[bot]`).

---

## 1. How Attribution Works

From the official docs — *"Authenticating with a GitHub App on behalf of a user"*:

> Your app can make API requests on behalf of a user. API requests made by an app on behalf of a user will be **attributed to that user**. For example, if your app posts a comment on behalf of a user, the GitHub UI will show the **user's avatar photo along with the app's identicon badge** as the author of the issue.

Key points:

- The UI shows the **user's avatar + the app's identicon badge** (a small bot marker) as the author.
- Audit/security logs list the **user as the actor**, with `programmatic_access_type` = `"GitHub App user-to-server token"`.
- The user must **authorize** the app before the app can act on their behalf.
- If the app is installed on an org with multiple members, **each member must authorize** individually.
- The app does **not** need to be installed for a user to authorize it.

---

## 2. Installation Tokens vs. User Access Tokens

| | Installation token (current code) | User access token (new capability) |
|---|---|---|
| **Type** | Server-to-server (S2S) | User-to-server (U2S) |
| **Prefix** | `ghs_...` | `ghu_...` |
| **Attribution** | Posts as the **bot** (`[bot]`) | Posts as the **user** (with app badge) |
| **Obtained via** | JWT → `POST /app/installations/{id}/access_tokens` | OAuth web/device flow |
| **Permissions** | App's installation permissions | Intersection of **user's** access + **app's** permissions |
| **Lifetime** | ~1 hour | 8 hours (refresh token: 6 months) |
| **Use case** | CI, automation, bot actions | "Post as this user" |

---

## 3. The OAuth Flow (How to Get a User Access Token)

There are **two** supported flows. Both require the app to have a **Client ID** and **Client Secret** (found in the app settings page — note the Client ID is *different* from the App ID).

### 3a. Web Application Flow (browser-based)

1. **Direct the user to:**
   ```
   https://github.com/login/oauth/authorize?client_id=CLIENT_ID&state=RANDOM_STRING
   ```
   Optional params: `redirect_uri`, `state` (anti-CSRF), `code_challenge`/`code_challenge_method` (PKCE, recommended), `login`, `prompt`.

2. **User authorizes** → GitHub redirects to your callback URL with a `code` query param.

3. **Exchange the code for a token** (server-side):
   ```
   POST https://github.com/login/oauth/access_token
   ```
   Params: `client_id`, `client_secret`, `code`, `redirect_uri`, `code_verifier` (if PKCE).

   **Response:**
   ```json
   {
     "access_token": "ghu_...",
     "expires_in": 28800,
     "refresh_token": "ghr_...",
     "refresh_token_expires_in": 15897600,
     "scope": "",
     "token_type": "bearer"
   }
   ```

### 3b. Device Flow (headless / CLI — best fit for this project)

Since this project is a headless webhook agent, the **device flow** is the most appropriate. It must be **enabled in the app settings** first.

1. **Request a device code:**
   ```
   POST https://github.com/login/device/code?client_id=CLIENT_ID
   ```
   Response includes: `device_code`, `user_code` (e.g. `WDJB-MJHT`), `verification_uri` (`https://github.com/login/device`), `expires_in` (900s), `interval` (5s).

2. **Prompt the user** to enter the `user_code` at `https://github.com/login/device`.

3. **Poll for the token** (respecting `interval`):
   ```
   POST https://github.com/login/oauth/access_token
   ```
   Params: `client_id`, `device_code`, `grant_type=urn:ietf:params:oauth:grant-type:device_code`.

   Poll until success or `expired_token`. Handle `authorization_pending` (keep polling) and `slow_down` (add 5s to interval).

### 3c. Auto-authorize on install (optional)

If the app setting **"Request user authorization (OAuth) during installation"** is enabled, GitHub starts the web flow immediately after install. This only covers the installing user; other org members still need the web/device flow.

---

## 4. Using the User Access Token

Once you have a `ghu_...` token, make API requests with it in the `Authorization` header:

```bash
curl --request POST \
  --url "https://api.github.com/repos/OWNER/REPO/issues/1/comments" \
  --header "Accept: application/vnd.github+json" \
  --header "Authorization: Bearer ghu_..." \
  --header "X-GitHub-Api-Version: 2026-03-10" \
  --data '{"body":"This comment is attributed to the user!"}'
```

The comment will appear authored by the user (with the app's identicon badge).

---

## 5. Critical Access Limitations

The user access token is **limited to the intersection** of what the user and the app can access:

1. **User's access:** The app can only access resources the user can access. If the user can't access a repo, the app can't either — even if the app is installed there.
2. **App's permissions:** The app can only use permissions it was granted. If the app lacks the `Issues` permission, it can't create/read issues for the user.
3. **Installation scope:** The app can only access resources in accounts where it's **installed**. If installed only on a user's personal account, it can't touch an org the user belongs to unless also installed on that org.

> **Important:** A user access token **cannot grant additional access** to a user. It only ever has the *intersection* of user + app permissions.

---

## 6. Token Lifecycle & Security

- **Expiration:** User access tokens expire after **8 hours** by default (expiring tokens are an optional feature — must be opted in).
- **Refresh tokens:** Expire after **6 months**. Use `POST /login/oauth/access_token` with `grant_type=refresh_token` to regenerate.
- **Revocation:** Users can revoke authorization. The app receives the `github_app_authorization` webhook (cannot unsubscribe). Stop using the token immediately; continued use returns `401 Bad Credentials`.
- **Storage:** Keep tokens/refresh tokens secure (e.g., encrypted at rest, not in source control).

---

## 7. How This Maps to the Current Codebase

The current implementation (`src/webhook_agent/github_credential_helper.py`) only supports **installation tokens**:

- `generate_jwt(app_id, private_key_pem)` — creates the App JWT (RS256).
- `get_installation_token(jwt, installation_id)` — exchanges JWT for a `ghs_...` installation token.
- Caches tokens in `~/.cache/github_app_helper/`.

To add "post on behalf of a user," the following would be needed (design sketch, not implemented):

1. **New env vars / secrets:** `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET` (Client ID ≠ App ID).
2. **New helper functions** in `github_credential_helper.py` (or a new module):
   - `get_device_code(client_id)` → `POST /login/device/code`
   - `poll_user_access_token(client_id, device_code)` → `POST /login/oauth/access_token`
   - `refresh_user_access_token(client_id, refresh_token)` → regenerate `ghu_...`
   - `get_user_access_token(client_id, code, client_secret)` → web flow exchange
3. **Token storage:** Cache user tokens keyed by user (e.g., `~/.cache/github_app_helper/user_token_{login}.json`), storing `access_token`, `refresh_token`, `expires_at`.
4. **Attribution decision:** Choose per-action whether to use the installation token (post as bot) or a user token (post as user).
5. **Loop-avoidance:** `bot_identity.py` currently detects bot-originated events. User-attributed events will have the **user** as sender with `performed_via_github_app` set — the existing `_is_bot_event` logic already checks `performed_via_github_app`, so it should still correctly identify app-originated events.

---

## 8. Addressing the Real Motivation: @-Mentioning & Assigning to a GitHub App

> **Context:** This research was originally motivated by the belief that you cannot @-mention a GitHub App or assign issues/PRs to it. That belief is **partially correct**, but the "post on behalf of a user" approach does **not** solve the triggering problem. Here's the breakdown.

### 8a. Can you @-mention a GitHub App?

**GitHub does not support formal @-mentions of GitHub Apps.** The official docs state you can mention "a person or team" — GitHub Apps are not in that list, and typing `@hannibal-hub-agents` will not render as a clickable mention or trigger a notification to the app.

**However, this codebase already works around this.** The webhook agent detects `@hannibal-hub-agents` by **string-matching the comment body**, not via GitHub's mention system:

- `src/webhook_agent/webhook_agent.py` → `_select_model_for_event()` checks if `"@hannibal-hub-agents"` appears in the comment body and routes it to the primary model.
- The `issue_comment.created` webhook fires regardless of whether the text is a formal mention.

**So users CAN trigger the agent** by typing `@hannibal-hub-agents` in a comment — it just won't show as a highlighted mention or send a notification. The agent still sees it and responds.

### 8b. Can you assign an issue/PR to a GitHub App?

**No.** The assignment docs confirm assignees are limited to **users**: yourself, anyone who commented, anyone with write access, and org members with read access. GitHub Apps are **not assignable** to issues or PRs.

### 8c. Does "post on behalf of a user" solve the triggering problem?

**No.** User-to-server tokens change **attribution** (who the post appears from), not **triggering** (how the agent gets invoked). The agent is triggered by webhooks, not by being mentioned or assigned. Posting as a user would not make the app mentionable or assignable.

### 8d. Recommended alternatives for triggering the agent

Since the agent is webhook-driven, the practical ways to trigger it are:

1. **@-mention in a comment (already works):** Type `@hannibal-hub-agents` in an issue/PR comment. The webhook fires and the agent string-matches it. This is the current mechanism.
2. **Slash commands (already works):** `/review`, `/create`, `/resolve`, `/help` are detected in comment bodies.
3. **PR lifecycle events (already works):** `pull_request.opened`, `ready_for_review`, `review_requested`, etc.
4. **Labels (potential):** The processor already routes `label.created` events — a dedicated trigger label (e.g., `agent-run`) could be added.
5. **A dedicated "trigger" user account (alternative):** If the goal is to have a human-like identity that can be @-mentioned and assigned, a **bot user account** (a real GitHub user, not an app) could be used. But this is generally discouraged vs. GitHub Apps and has its own limitations (e.g., no fine-grained permissions, shared-credential risk).

### 8e. When "post on behalf of a user" IS useful

The user-to-server capability is valuable for a different reason: **attribution**. If you want the agent's comments/reviews to appear as a specific human user (e.g., a team member) rather than the bot, user access tokens achieve that. But it does not change how the agent is triggered.

---

## 9. Key Takeaways

- ✅ **Yes**, a GitHub App can post on behalf of a user — via **user-to-server** requests with a `ghu_...` user access token.
- The user must **authorize** the app (web flow or device flow).
- Attribution shows the **user's avatar + app's identicon badge**.
- The token is limited to the **intersection** of user access and app permissions.
- The **device flow** is the best fit for this headless webhook agent.
- This is a **separate capability** from the current installation-token (bot) flow; both can coexist.
- ⚠️ **Important:** "Post on behalf of a user" does **not** make a GitHub App @-mentionable or assignable. The agent is triggered via **webhooks**, and the codebase already supports triggering via `@hannibal-hub-agents` string-matching in comments.

---

## 10. References

- [Authenticating with a GitHub App on behalf of a user](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-with-a-github-app-on-behalf-of-a-user)
- [Generating a user access token for a GitHub App](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app)
- [Refreshing user access tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/refreshing-user-access-tokens)
- [Authenticating as a GitHub App installation](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation)
- [Rate limits for GitHub Apps](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api?apiVersion=2026-03-10#rate-limits-for-github-apps)