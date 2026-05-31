# Cursor Handoff — Podcast and Blogs Page

Drop these two files into your codebase and Cursor will build the page in one shot.

## What's in this folder

```
handoff/
├── PodcastAndBlogsPage.tsx     ← drop-in replacement
└── public/
    └── podcast-photo.png        ← episode cover image
```

## 1. Replace the component file

Overwrite the existing file at:

```
src/app/components/PodcastAndBlogsPage.tsx
```

with `handoff/PodcastAndBlogsPage.tsx`.

That's it for code. No new dependencies, no new imports. All styles are inline in the same `<style>{styles}</style>` pattern your existing file uses. The export signature is identical (`export default function PodcastAndBlogsPage()`) so `App.tsx` does not need to change.

## 2. Add the podcast cover image

Copy `handoff/public/podcast-photo.png` to your public folder so it's served from the site root:

```
public/podcast-photo.png
```

The component references it as `/podcast-photo.png` — same convention as your existing `/team-whitepaper.pdf` and `/hippocrates-email-bg.png`.

## 3. (Already in place) Whitepaper PDF

The whitepaper "Read paper" / "Download PDF" buttons point to `/team-whitepaper.pdf`. This file already exists in your repo — no change needed.

## 4. Nav order

The `SiteHeader` tab order has been requested as: **Home → TEAM calculator → Podcast and Blogs**. If `SiteHeader.tsx` currently renders them as Home → Podcast and Blogs → TEAM calculator, swap the order of the last two `<a>` tags in `site-header-nav`. The component itself does not control this.

---

## Cursor prompt (paste this)

> Replace `src/app/components/PodcastAndBlogsPage.tsx` with the file at `handoff/PodcastAndBlogsPage.tsx`. Copy `handoff/public/podcast-photo.png` to `public/podcast-photo.png`. In `src/app/components/SiteHeader.tsx`, reorder the nav tabs to: Home, TEAM calculator, Podcast and Blogs. Do not modify any other file. Do not install any packages. Do not add new imports. The export signature of `PodcastAndBlogsPage` is unchanged, so `App.tsx` does not need to change.

---

## Behavior

- **Two tabs**: "Podcast" and "White Paper". Tab pill matches the existing cyan-on-dark pattern.
- **Podcast tab**: full-bleed cover image, centered cyan play button (104px), bottom-overlay metadata: `EP.05 · 47:12`, episode title, guest line, decorative waveform.
- **White Paper tab**: dark navy card with a glowing cyan heart + EKG SVG anchored top-right; title, audience pill, and primary "Read paper" + ghost "Download PDF" CTAs on the left. The veil gradient protects text legibility regardless of the SVG behind it.
- **Data**: driven by `PODCAST_EPISODES[]` and `WHITEPAPERS[]` arrays at the top of the file — currently one entry each. Add more entries to extend; the design currently shows `[0]`.

## Customizing later

- Episode title, guest, duration, date → edit `PODCAST_EPISODES[0]` in the file.
- Whitepaper title, audience, page count → edit `WHITEPAPERS[0]`.
- Image swap: replace `/podcast-photo.png` in `public/`. The `<img>` uses `object-fit: cover` so any 16:9-ish image works.
- The heart + EKG art is a self-contained `<HeartEKGArt />` component drawn in SVG. Resolution-independent — tweak `transform="translate(660 140) scale(0.62)"` to move/resize it.

## Removed from the original

- The `TeamWhitepaperPage` import and inline PDF viewer were removed (the design no longer renders the PDF inline — it opens via the CTAs instead). If you want it back, re-import `TeamWhitepaperPage` and render it below the whitepaper hero conditionally.
- `PODCAST_EPISODES` lost the `videoUrl`, `showNotesUrl`, `tags`, `topic`, and `summary` fields since the simplified design does not surface them. Add them back to the type and data if you want to expose them later.
