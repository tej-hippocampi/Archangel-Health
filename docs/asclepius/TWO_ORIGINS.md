# The landing page and the portal are different origins

```
landing SPA   https://archangelhealth.ai        Vercel, static
portal        https://app.archangelhealth.ai    FastAPI on Railway
```

Everything below follows from that one line, and it has now caused the same
bug twice.

## What went wrong

A physician finished signing up, was told their workspace was ready, clicked
through, and got a login screen. Every time, for everyone.

The wizard called `storeAsclepiusSession(token)`, which wrote
`localStorage["asclepius_token"]`. The portal's `boot()` reads that exact key.
The docstring said *"the wizard shares the same origin as /asclepius so the
value carries over"*, which is true in local development, where both are served
off `:8000`, and false in production, where localStorage is partitioned by
origin. The portal found nothing and fell through to `renderLogin()`.

It survived review because the round before had fixed a *different* bug in the
same flow: `finish` was calling `authenticate(..., director_pwd)` with a name
that did not exist in the function, the `NameError` was swallowed by a bare
`except`, and the token came back `None`. Fixing that made the token correct
and left the transport broken, so the symptom did not move.

**Do not try to write a token across these two origins.** There is no cookie
domain, no `postMessage` shim and no localStorage trick that is worth the
trouble. Use the handoff.

## The handoff

Already built, already in use by `SignInDialog` and `ResetPasswordPage`:

```
landing                                    portal
  |                                          |
  |-- POST /api/asclepius/auth/portal-handoff
  |     Authorization: Bearer <asc token>    |
  |<-- { handoff_code, expires_in_seconds }  |
  |                                          |
  |-- redirect to /asclepius?asc_handoff=CODE -->
  |                                          |
  |     POST /auth/portal-handoff/consume ---|
  |     <-- { token }  (single use, 60s TTL) |
```

The code is server-held, single-use and expires in 60 seconds. The raw JWT never
enters a URL, so it never enters browser history, server access logs, or a
`Referer` header.

Client side that is one line: `authApi.redirectToAsclepiusPortal(token)`.
Portal side: `consumeHandoffFromUrl()`, the first thing `boot()` awaits.

`storeAsclepiusSession` was deleted rather than left unused, because it looks
exactly like the thing you want and the next person will reach for it.

## The other half: already having an account

`GET /api/onboarding/session` returns `account_exists` when the invite's address
already has an Asclepius account with a password, and the wizard shows a sign-in
screen instead of a signup.

This is not cosmetic. Walking the wizard a second time reaches
`/asclepius/finish`, which passes `password_hash=` unconditionally, so the run
**silently repointed the live account's password** to whatever got typed on the
way through. Two ordinary things produce that: a colleague forwards a `/join`
link to someone who signed up months ago, and a physician requests a fresh link
having forgotten they already have one.

Not an account-existence oracle: the caller already holds a signed onboarding
token minted for that exact address, so they cannot ask about anybody else's.

## Anything else served from the portal origin

Profile pictures are bearer-authenticated (`GET /users/{id}/avatar`), and an
`<img src>` cannot carry an `Authorization` header. Fetch the bytes with the
session token and hand the element a `blob:` URL. `community.js` has done this
for attachments since the start; `loadAvatarBlob` in `asclepius.js` is the same
shape and is shared with the admin sections through `adminSectionCtx()`.

If you find yourself about to set `src` to a portal API path, that is the
symptom.
