# Email and product assets

Served at `/email-assets/<file>` by the mount in `main.py`. Everything here is
public: an email client fetches it with no session, so nothing private belongs
in this directory.

## Why no image is committed

Three files are referenced by name below and none of them is in git, on
purpose, and the reason is not only privacy:

* they are photographs of real people, and git history is permanent;
* `/email-assets` is public and unauthenticated, so a committed file is a
  guessable public URL from the moment it deploys;
* **a blank placeholder is worse than no file at all.** Every consumer here
  degrades to something deliberate when the image is missing (initials, or the
  names alone, or no `<img>` element), and all three of those read better than
  a grey rectangle where a face should be.

So the wiring is complete and the files are the only thing outstanding. Drop
them in at the paths below on the server or a mounted volume, or point the
matching environment variable somewhere else.

## The three files

| Path | Size | Used by | If absent |
|---|---|---|---|
| `founders.jpg` | 224x224 square | `onboarding_emails._founder_signoff`, the signature of every onboarding email | The signature renders the names alone |
| `community-persona.png` | 512x512 square | `community/persona.py`, the Archangel account's avatar everywhere in the community | The account renders as initials |
| `founders-wide.jpg` | 1200x675, 16:9 | The founder card on the applicant's welcome screen and dashboard (`asclepius.js`, `founderStripEl`) | No `<img>` is emitted and the card renders text only |

`founders.jpg` is displayed as a 56px circle, so keep the faces centred and
supply it at 4x for retina. `community-persona.png` is centre-cropped square by
`asclepius.avatar.store()` anyway; supplying it square means the crop is yours.
`founders-wide.jpg` is the only one that can hold three people, which is why the
intro card uses its own file rather than the square one.

`BASE_URL` must be set for the email signature, because an email is read outside
our origin and a relative URL resolves against the mail client.

## Environment overrides

| Variable | Points at |
|---|---|
| `FOUNDER_PHOTO_URL` | A hosted `founders.jpg`, instead of this directory |
| `COMMUNITY_PERSONA_AVATAR` | A path to the persona image, instead of this directory |
| `COMMUNITY_PERSONA_NAME` | What the account is called. Setting it at all switches the initials from the pinned `AH` to letters derived from the name |
