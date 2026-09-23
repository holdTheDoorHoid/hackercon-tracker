# Connecting Gmail (one-time, about 10 minutes)

The app talks to Gmail through Google's official API. Google requires each app to have
its own "OAuth client", which only you can create. The app never sees your password.

## 1. Create a Google Cloud project

1. Go to https://console.cloud.google.com/ and sign in with the Gmail account you use for
   conference outreach.
2. Top bar → project picker → **New project**. Name it `Hackercon Tracker`. **Create**, then
   make sure it is selected in the picker.

## 2. Turn on the Gmail API

1. Left menu → **APIs & Services → Library**.
2. Search **Gmail API** → open it → **Enable**.

## 3. Set up the consent screen

1. **APIs & Services → OAuth consent screen** (Google may call this **Google Auth Platform →
   Branding / Audience**).
2. User type: **External** → Create.
3. App name `Hackercon Tracker`, your email as the support email and developer contact.
   Save.
4. **Audience** (or "Test users" on older layouts): add your own Gmail address as a test user.
5. **Publishing status**: click **Publish app** (so it says "In production").
   This matters: while an app is in "Testing", Google expires the sign-in every 7 days and
   you'd have to reconnect weekly. Publishing does not require verification for personal use;
   you'll just see an "unverified app" warning once when you connect (click *Advanced →
   Go to Hackercon Tracker*).

## 4. Create the OAuth client

1. **APIs & Services → Credentials → + Create credentials → OAuth client ID**.
2. Application type: **Desktop app**. Name: `Hackercon Tracker`. **Create**.
3. Click **Download JSON**.
4. Save that file as `data/credentials.json` inside the project folder
   (`hackercon-tracker/data/credentials.json`).

## 5. Connect

1. Start the app (`./start.sh`), open **Settings**, click **Connect Gmail**.
2. Pick your account, accept the warning about an unverified app, allow the permissions.
   The only permission asked for is "read, compose, send, and permanently delete all your
   email" — that is Google's wording for the `gmail.modify` scope; the app never deletes
   anything.
3. You land back in the app. Go to **Email → Sync Gmail now**.

The first sync looks back two years (change the window in Settings) and pulls mail that
mentions a tracked conference, comes from a known organizer domain, or carries an
`Cons/…` label (the prefix is a setting). It then keeps a local copy so the app stays fast and works offline.

## If something goes wrong

- **"redirect_uri_mismatch"**: the OAuth client must be type *Desktop app*. If you created
  a *Web application* client, either recreate it as Desktop, or add
  `http://localhost:8765/gmail/callback` to its authorized redirect URIs.
- **"Access blocked: app has not completed verification"**: add yourself under
  *Audience → Test users*, or publish the app (step 3.5).
- **Sign-in keeps expiring after a week**: the app is still in Testing; publish it.
- **Start over**: delete `data/token.json` and click Connect Gmail again.
