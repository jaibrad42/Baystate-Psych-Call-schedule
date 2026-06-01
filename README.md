# Baystate Psychiatry Call Scheduler

A web app for generating and managing the Baystate Psychiatry residency call schedule.

## Deploy to Streamlit Cloud (free, 5 minutes)

### Step 1 — GitHub repo
1. Go to https://github.com and sign in (or create a free account)
2. Click **New repository** → name it `baystate-scheduler` → Private → Create
3. Upload these three files to the repo:
   - `streamlit_app.py`
   - `config.json`
   - `requirements.txt`

### Step 2 — Streamlit Cloud
1. Go to https://share.streamlit.io and sign in with GitHub
2. Click **New app**
3. Select your `baystate-scheduler` repo
4. Main file: `streamlit_app.py`
5. Click **Deploy**

That's it. You'll get a URL like `https://baystate-scheduler.streamlit.app` that anyone can open in a browser — no install required.

### Step 3 — Private access (optional)
In Streamlit Cloud settings you can restrict access to specific email addresses or require a password, so only your program can use it.

---

## How config.json works in the web app

The app loads `config.json` from the repo as its starting point. Changes made in the app (new residents, no-call dates, holidays) are stored in the browser session — they're not automatically saved back to GitHub.

To save changes permanently:
1. Use **💾 Download config.json** in the sidebar to download your updated config
2. Upload that file back to your GitHub repo (replacing the old one)
3. The app will reload with the updated data

This is intentional — it keeps your resident data in your own GitHub repo, not on Streamlit's servers.

---

## Run locally (optional)
```bash
pip install streamlit openpyxl
streamlit run streamlit_app.py
```
