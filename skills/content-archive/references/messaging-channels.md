# Messaging Channels

Use when the user wants to forward links from a phone or receive the digest outside the desktop app. Hermes ships native adapters; do not build a custom bridge. Which channels an instance actually uses is its own decision, recorded in its README — do not assume a default.

## Channel choice

| Channel | Transport | Reliability | Best use |
|---|---|---|---|
| Feishu / Lark | WebSocket long connection | High | Private group + bot; phone forwarding; scheduled home chat |
| WeCom | AI Bot WebSocket | High | Organization already standardized on WeCom |
| Weixin (personal) | iLink Bot long polling | Medium-low | DM forwarding only; secondary account |
| DingTalk / QQ / Yuanbao | Native adapters | Medium | Fallback or existing org channel |

## Feishu / Lark

- Run `hermes gateway setup`, select Feishu / Lark, scan the QR code.
- Required scopes: `im:message`, `im:message:send_as_bot`, `im:resource`, `im:chat`, `im:chat:readonly`.
- DMs: the bot replies to every message. Groups: the bot replies when @mentioned.
- Create a private group with the user and the bot; it becomes the mobile intake surface.
- Send `/set-home` in the target chat to make it the scheduled-notification home channel, or pin it explicitly with `FEISHU_HOME_CHANNEL=oc_...`.
- Supports images, files, voice, threads, reactions, typing, and streaming.

## WeCom

- Requires an organization account and an AI Bot created in the WeCom Admin Console.
- Run `hermes gateway setup` and scan to create, or set `WECOM_BOT_ID` / `WECOM_SECRET` manually.
- Supports DMs and groups, media, markdown, and streaming; no threads or reactions.
- `WECOM_HOME_CHANNEL` pins the notification chat; `WECOM_ALLOWED_USERS` restricts access.
- Admin policy may limit obtainable scopes; confirm before promising group use.

## Weixin (personal WeChat)

- The adapter logs in through Tencent's iLink Bot API and acts as a bot identity, not the user's ordinary personal account.
- QR login: `hermes gateway setup` → Weixin; scan with the WeChat mobile app and confirm.
- DMs to the bot identity are the reliable path. Ordinary WeChat groups usually do not deliver events to the bot; group policy defaults to `disabled`.
- `WEIXIN_ACCOUNT_ID` is the minimum environment value; `WEIXIN_HOME_CHANNEL` pins notifications.
- Use a secondary account. Personal-account automation carries account risk, and group support must not be promised.

## Routing: messaging sessions are not desktop projects

The gateway uses the profile's `terminal.cwd`, not a per-chat project workspace. Two workable patterns:

**A. Single profile + absolute archive path (start here).** Keep the current profile; the skill and CLI resolve the archive root by absolute path, so every bot session writes to the same archive. Simple; isolation is procedural.

**B. Dedicated profile (for daily mobile use).** Create a separate profile whose `terminal.cwd` is the archive root and give it its own bot credentials. Hard isolation; extra model/skill/memory configuration.

The desktop project remains the primary conversation. Messaging surfaces are additional intake and delivery channels writing to the same Markdown archive; they are not the same session.

## Scheduled delivery

- **Single or symmetric payload:** route the job's `deliver` to the platform's home channel, or to an explicit `platform:chat_id` for one chat. Use `attach_to_session: true` for a replyable digest.
- **Asymmetric dual-channel delivery** (for example a full digest by email plus a character-capped summary on a chat platform, with no cross-channel fallback):
  - A comma-separated `deliver` list sends identical text to every target, and a standalone send command uses a fixed default subject. To send different payloads per channel without duplicating the schedule, set the job to deliver locally (`attach_to_session: false`, no `failure_deliver` when the user opted out of fallbacks) and run a dedicated delivery step at the finish checkpoint, and on the failure path when failure alerts are enabled.
  - **Email lane:** read the SMTP variables the runtime already provides, never log or persist them, and build a multipart alternative message (plain text with emphasis stripped, HTML with clickable links) with an explicit dated subject. Where direct SMTP egress is blocked by container or network policy, tunnel TCP through the configured HTTP proxy with a CONNECT request and pass the real SMTP host — not the proxy address — as the TLS server name.
  - **Chat lane:** enforce the character budget in code (`len(text) <= max_chars`) by trimming the displayed item count and title length before appending the tail line. Retry once after a short delay only for transient rate-limit cooldowns, never for session-not-ready or resolution failures.
  - **Idempotency and accounting:** persist per-channel `ok|failed` in the gate file under the state lock so reruns skip channels that already succeeded, and exit non-zero when a required channel failed rather than cross-sending a fallback the user declined.
- Test with a dry run or a loopback fixture before enabling live scheduled sends.

## Security

- Allowlist user IDs; prefer pairing mode or a private group.
- Keep bot secrets in the environment file, never in the archive, the repository, or a chat message.
- Grant the minimum scopes; do not install the bot in public groups.
- Personal WeChat: secondary account only; expect occasional disconnects and group limitations.
- A digest sent to a group is a publication decision: check with the owner before adding recipients who are not the archive owner.
