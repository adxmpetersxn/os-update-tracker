# Mobile OS release tracker

Daily job that watches Apple and Android developer feeds, emails you a digest of new
milestones (with a suggested action for each), and publishes an `.ics` calendar.

## Setup (about 15 minutes)

1. **Create a repo** named `os-update-tracker` under your GitHub account and upload
   everything in this folder, including the hidden `.github` folder.
   - Use a **public** repo if you want the calendar feed via GitHub Pages on a free plan.
     Everything the tracker stores comes from public feeds, so nothing sensitive lands in it.
     Your email address is kept in a secret, not in the repo.
2. **Gmail app password:** turn on 2-Step Verification for the Gmail account, then create an
   app password at https://myaccount.google.com/apppasswords.
3. **Add repo secrets** (Settings → Secrets and variables → Actions):
   - `GMAIL_USER`: the Gmail address the mail is sent from
   - `GMAIL_APP_PASSWORD`: the 16-character app password
   - `ALERT_EMAIL`: where alerts go (your address)
4. **Enable Pages:** Settings → Pages → Deploy from branch → `main` → `/docs`.
5. **First run:** Actions tab → "OS update tracker" → Run workflow. The first run loads a
   baseline of existing items and sends one "initialized" email instead of a flood.
6. **Subscribe in Outlook:** Calendar → Add calendar → Subscribe from web, and paste
   `https://<your-username>.github.io/os-update-tracker/os-calendar.ics`

## Day-to-day

- **Confirm a date:** edit `data/events.json` and change `"status": "proposed"` to `"confirmed"`.
  The `[Proposed]` prefix disappears from the calendar on the next run.
- **Add hard dates** (Google Play target API deadline, Apple SDK requirement, your own
  test windows) in `manual_events` in `config.json`:
  ```json
  "manual_events": [
    {"title": "Google Play target API deadline", "date": "YYYY-MM-DD", "os": "Android",
     "url": "https://developer.android.com/google/play/requirements/target-sdk"}
  ]
  ```
  You get reminders 30, 7, and 1 day before (`reminder_days` in config).
- **Tune the actions** in the `actions` block to match your real testing policy.

## Things to know

- The feed URLs and title patterns are rule-based guesses at how Apple and Google name
  things. Check the first few real emails and adjust `config.json` if something is
  misclassified or missed.
- If a source fails 3 days in a row you get a warning email.
- Failed email sends don't mark items as seen, so they retry the next day.
- Outlook can take hours to refresh a subscribed calendar. Treat email as the timely channel.
- Scheduled GitHub workflows can be paused on repos with no activity for a long stretch.
  If the emails stop, check the Actions tab.
