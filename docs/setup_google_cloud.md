# Setup: Google Cloud service account for the GA4 Data API

One-time setup (~20 min). You need admin access to the GA4 property.

## 1. Google Cloud project & API

1. Go to https://console.cloud.google.com → project selector → **New project** (e.g. `ga4-reporting`). No billing required for this API.
2. With the project selected: **APIs & Services → Library** → search **"Google Analytics Data API"** → **Enable**.

## 2. Service account + key

1. **APIs & Services → Credentials → Create credentials → Service account.**
2. Name it (e.g. `ga4-reader`) → Create → skip the optional role steps → Done.
3. Open the service account → **Keys** tab → **Add key → Create new key → JSON** → a `.json` file downloads. **Keep it private — it is a password.** Never commit it (this repo's `.gitignore` blocks it).

## 3. Grant it access in GA4

1. Copy the service account email (`ga4-reader@...iam.gserviceaccount.com`).
2. In https://analytics.google.com: **Admin → Property → Property access management → + → Add users** → paste the email → role **Viewer** → Add.

## 4. Find your property ID

**Admin → Property → Property details** → "Property ID" (a number like `123456789`).

## 5. Run locally

```bash
pip install -r requirements.txt
set GOOGLE_APPLICATION_CREDENTIALS=C:\path\to\your-key.json   # Windows
set GA4_PROPERTY_ID=123456789
python ga4_report.py
```

(Or test the pipeline without credentials: `python ga4_report.py --demo`)

## 6. Enable the weekly automation (GitHub Actions)

1. In the GitHub repo: **Settings → Secrets and variables → Actions → New repository secret.**
2. Secret `GA4_SA_KEY`: paste the **entire content** of the JSON key file.
3. Secret `GA4_PROPERTY_ID`: your property ID number.
4. **Actions** tab → "Weekly GA4 report" → **Run workflow** to test it immediately. It will commit `report/report.html` if everything is wired up.
