# Email assets

Served at `/email-assets/<file>` by the mount in `main.py`. Everything here is
public: an email client fetches it with no session, so nothing private belongs
in this directory.

## `founders.jpg` (not in the repo)

The founders' photo shown above their names in the signature of every
onboarding email (`onboarding_emails._founder_signoff`). It is deliberately
optional, and absent by default:

* a remote image in an email is blocked by most clients until the reader allows
  it, and a BROKEN image is worse than no image, so the signature degrades to
  the names alone when this file is missing;
* it is a photo of real people and does not belong in git history.

To turn it on, either drop a square JPEG here as `founders.jpg`, or set
`FOUNDER_PHOTO_URL` to a hosted one. `BASE_URL` must also be set, because an
email is read outside our origin and a relative URL resolves against the mail
client.

Aim for roughly 224x224 (4x the 56px it renders at, for retina). It is
displayed as a circle, so keep the faces centred.
