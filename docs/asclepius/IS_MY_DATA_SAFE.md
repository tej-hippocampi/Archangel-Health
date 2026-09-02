# Is my data safe? — the volume, in the right order

The one failure that makes every other guarantee irrelevant: if the Railway
volume is not attached, **every account, task, submission, review and payout row
is erased on the next redeploy.** Nothing errors. The screens just go quiet,
which reads as "nobody signed up" rather than "the database was deleted."

This is a **configuration** problem, not a code one. Twenty minutes, once.

---

## First: find out where you stand

Sign in to the admin console. If there is a **red or amber banner at the top of
every tab**, it names each store that is not safe and what to set. If there is
no banner, storage is durable *and* the fail-closed gate is armed — you are
done, and this page is only here for the next time something changes.

The same answer as JSON, if you prefer:

```
GET /api/asclepius/admin/storage/durability
```

```jsonc
{
  "all_durable": false,
  "gate_armed":  false,
  "volume_mount": null,          // Railway sets this only when a volume exists
  "stores": [
    { "store": "Asclepius database", "durable": false,
      "detail": "ASCLEPIUS_DB_PATH is not set, so the database lives beside the
                 code at /app/backend/asclepius.db and is replaced on every
                 redeploy." },
    …
  ],
  "remedy": "Attach a volume mounted at /data, …"
}
```

`volume_mount: null` is the fastest tell: Railway injects
`RAILWAY_VOLUME_MOUNT_PATH` into every service that has a volume. Null means
there is no volume at all.

---

## The fix, in order — the order matters

### 1. Attach the volume

Railway → your service → **Settings** → **Volumes** → **New Volume**.
Mount path: `/data`. (Anything except `/app`, which is the code.)

### 2. Point the four stores into it

Service → **Variables**:

| Variable | Value | What is destroyed without it |
|---|---|---|
| `ASCLEPIUS_DB_PATH` | `/data/asclepius.db` | Every physician account, task, submission, review, earning and payout row |
| `TEAM_DB_PATH` | `/data/team.db` | Every patient episode, intake form, care-team message, audit record, and every physician mid-onboarding |
| `ASCLEPIUS_DATA_DIR` | `/data` | V4 case images (the asset store lands at `/data/assets`) |

The raw-ingest directory needs no variable — it defaults to sitting beside
`ASCLEPIUS_DB_PATH`, so setting that carries partner uploads onto the volume
too.

> **Saving these triggers a redeploy, and that redeploy is what destroys the
> current container's data.** Anything already written to the old ephemeral
> database is gone at that moment; the new container starts with an empty
> database on the volume. There is no migration step, because there is nothing
> durable to migrate *from*. **If the current data matters, copy it out first** —
> `railway ssh`, then `cp /app/backend/asclepius.db /app/backend/team.db /tmp/`
> and download them — before you save the variables.

### 3. Confirm all four are green

Reload the admin console. **The banner should be gone.** In the deploy log:

```
[storage] all three stores durable
[storage] tenant database durable (/data/team.db)
```

If any store is still red, the banner names it and what to set. Fix that before
step 4 — see the warning below.

### 4. Arm the gate: `ENV=production`

This is the step that actually *solves* the problem rather than checking it.
With `ENV=production` the app **refuses to boot** on non-durable storage.

That converts the failure mode from *"silently accepts data it will destroy"*
into *"a five-minute incident with a log line naming the store"*. A container
that will not start is loud. A container quietly deleting your database is not.

> **Set this only after step 3 is green.** Arm the gate while a path is still
> wrong and the service will not boot — which is the gate working exactly as
> designed, but it is a confusing way to meet it.

---

## Why the order is the whole point

Steps 1–3 make today safe. **Step 4 is what keeps it safe.**

Without the gate, storage can silently become ephemeral again on any future
change — a typo in a variable, a volume detached while debugging, a new service
created from the old template — and nothing will stop the app from accepting
data it is going to destroy. That is why the console shows an amber banner when
everything is durable but `ENV` is unset: today's green is luck, not a
guarantee, and the banner is asking you to convert one into the other.

---

## What this does and does not cover

**Covers:** the volume being absent, unmounted, mounted read-only, or pointed at
by the wrong variable — checked live, at boot and on every admin page load, for
all four stores.

**Does not cover:** an actual backup. A volume is one copy on one disk. It
survives redeploys; it does not survive the volume being deleted, a bad
migration in some future change, or an operator running the wrong `DELETE`.
Railway can snapshot volumes — if the data in there is worth what it is worth to
you now, that is the next thing to set up, and it is a separate job from this
one.

**Also separate:** `DATA_ENCRYPTION_KEY` (PHI field encryption at rest — the
boot log warns when it is unset) and `docs/security/ENCRYPTION.md`.
