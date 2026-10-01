# VaanAI — Open-Source Build Plan (Python/API-literate, new to shipping & hosting apps)

You already know Python, VBScript, and how APIs work — so this plan skips coding fundamentals entirely. Your actual gap is the part self-taught devs usually never get taught on purpose: **taking a script that runs on your laptop and turning it into a real service** — one with a database, a schedule, a server it lives on, and a way to see when it breaks. Every phase below is built around closing that specific gap, using satellite data as the running example since that's the real project.

---

## How to use this plan

- Do the phases **in order** (0 through 8) — each introduces exactly one new "app-building" concept using code you already know how to write.
- Each day is one working thing, not a perfect thing.
- When a new concept shows up (Docker, environment variables, a "container," a "worker") treat it as a 15-minute mini-lesson before touching it — these are infrastructure concepts, not coding ones, and they're genuinely new even to strong Python developers who've only ever run scripts locally.

---

## Phase 0 — Local script → structured project (2–3 days)

The habits that separate "a script" from "an app."

- [ ] **Day 1** — Install **VS Code**, **Git**, and set up a **GitHub** account/repo for VaanAI. If you've never used git: it's version control — think "track changes" for code, with the ability to undo and to work from any machine. Learn `git add`, `commit`, `push`, `pull` — that's 90% of daily use.
- [ ] **Day 2** — Restructure any script you already have (or write a fresh one that hits one free NASA API endpoint) into a **proper project layout**: a `requirements.txt` (your dependency list), a `.env` file for secrets/API keys (never hardcode keys — this is the single most common beginner mistake that causes real problems), and a `.gitignore` that excludes `.env` from git.
- [ ] **Day 3** — Learn **virtual environments** (`venv`): why every project should have its own isolated Python environment instead of installing packages globally. Recreate your project inside one.

**New concept unlocked**: a project that isn't "one file on my laptop" anymore — it's something someone else could clone and run.

---

## Phase 1 — Give it a real database (Week 1)

- [ ] **Day 4** — Create a **Supabase** project (hosted Postgres — zero server setup, generous free tier). This is your first taste of a *managed service*: someone else runs the database server, you just connect to it.
- [ ] **Day 5** — From your script, use `supabase-py` (or plain `psycopg2` if you want to practice raw SQL) to create a table and insert rows. Learn the four basics: `INSERT`, `SELECT`, `UPDATE`, `DELETE` — you'll use these constantly.
- [ ] **Day 6–7** — Rewrite your NASA API script so that instead of printing results, it **writes them to the database**. Run it a few times manually and confirm rows accumulate correctly (watch for duplicate-row bugs — a classic first mistake, solved with a unique constraint or an "upsert").

**New concept unlocked**: your script now has persistent memory across runs.

---

## Phase 2 — Package it so it can run anywhere (Week 2)

This is the part that's genuinely new even for a solid Python developer: making code portable to a server.

- [ ] **Day 8** — Install **Docker Desktop**. Understand the core idea: a **container** is a lightweight box with your code, Python, and its dependencies bundled together, so it runs identically on your laptop and on a server. This solves the classic "works on my machine" problem.
- [ ] **Day 9** — Write a `Dockerfile` for your script (a short recipe: "start from Python, copy my code in, install requirements, run it"). Build it (`docker build`) and run it locally (`docker run`) — same output as running it directly, but now containerized.
- [ ] **Day 10** — Learn `docker-compose`: a way to describe *multiple* containers (e.g. your script + a local test database) and start them together with one command. You don't strictly need this since Supabase is hosted, but it's the standard tool and worth knowing early.

**New concept unlocked**: your code is now portable — the exact same container can run on a real server, not just your laptop.

---

## Phase 3 — Put it on a real server (Week 3)

- [ ] **Day 11** — Sign up for **Oracle Cloud Free Tier** (generous always-free VM) or **Railway**/**Render** (simpler, deploy-from-GitHub free tiers — better if you want to skip raw server administration for now). Either is fine; Railway/Render is the gentler on-ramp.
- [ ] **Day 12** — Deploy your container. On Railway/Render this is close to "connect GitHub repo, click deploy." On a raw VM (Oracle), you'll `ssh` in, install Docker, and `docker run` your image manually — a good exercise even if you move to a managed host later, since it teaches you what's actually happening underneath.
- [ ] **Day 13** — Set environment variables (your API keys, database URL) on the **hosting platform**, not in your code — this is how secrets work in production. Confirm the deployed version successfully writes to Supabase, same as it did locally.
- [ ] **Day 14** — Learn to read **logs** on your host (Railway/Render/Oracle all show you console output). This is how you'll debug a server that isn't sitting in front of you.

**New concept unlocked**: your script is now reachable and running somewhere that isn't your laptop.

---

## Phase 4 — Make it run on a schedule, unattended (Week 4)

- [ ] **Day 15** — Two options, pick one to start: (a) your hosting platform's own **cron/scheduled job** feature (Railway and Render both support this directly — simplest path), or (b) **n8n** (open-source, visual workflow tool) if you want a UI for chaining steps and adding alerts later. Given your Python background, (a) is likely faster for you specifically.
- [ ] **Day 16** — Set your script to run daily (or hourly) via the scheduler. Confirm it fires without you triggering it manually.
- [ ] **Day 17–18** — Add basic **alerting**: on failure, send yourself a message (simplest: a webhook to Telegram, or just an email via a free transactional email API like Resend). This is what "active the whole day" actually means in practice — a schedule plus a way to know when it silently stops working.

**New concept unlocked**: a service that runs itself, and tells you when it's broken.

---

## Phase 5 — Turn the raw data into an insight (Week 5)

- [ ] **Day 19–20** — Using `rasterio`/`numpy` (or your existing tools of choice), add the actual analysis step: pull two dated images of one AOI, compute a simple diff (e.g. NDVI change).
- [ ] **Day 21** — Call the **Claude API** with the computed stats, get back a written insight, and store it in a new `insights` table in Supabase.
- [ ] **Day 22–23** — Make this the new payload of your scheduled job: fetch → analyze → summarize → store. Same infra as Phase 4, new logic inside.

---

## Phase 2 — Turn data into an insight (Week 3)

**You now know**: how to go from raw pixels to an AI-written insight, stored in your database, on a schedule, unattended.

---

## Phase 6 — Build the website (Week 6)

- [ ] **Day 24–25** — Learn just enough HTML/CSS/JS, or start from a free open-source landing-page template (search "free open source HTML landing page template") and edit the text — given your background this will feel closer to scripting than "real" frontend dev.
- [ ] **Day 26** — Build one page: what VaanAI does, one sample insight, a simple email signup form. Have the form write to a Supabase table via a small JS `fetch` call — same database you're already using, no new backend needed.
- [ ] **Day 27** — Deploy the site for free using **GitHub Pages** or **Cloudflare Pages** — connect your GitHub repo, get a live URL in a few clicks.
- [ ] **Day 28** — Add a "Latest Insights" section pulling the newest rows from your `insights` table, so the site updates itself as your scheduled job runs — this is the moment the whole pipeline becomes visibly alive to anyone visiting the site.

**You now know**: a live website showing real, automatically generated insights, backed by the same infra you built in Phases 0–5.

---

## Phase 7 — Grow the pipeline (Week 7 onward)

Once Phases 0–6 work end to end for one area and one data type:

- [ ] Add a **second AOI** (area of interest) to your surface-change pipeline.
- [ ] Add a **second insight type** (debris via CelesTrak, or rare-event feed via NASA EONET) as a separate scheduled job — don't merge them yet, keep things simple and separate.
- [ ] Introduce **Metabase** (free, open-source, drag-and-drop dashboards) to visualize how many insights you're generating and where.
- [ ] Start writing down: which insight got the most interest when you show it to someone? That tells you where to focus next, more than any tech decision will.

## Phase 8 — When to "graduate" tools

Don't switch tools just because they sound more professional — switch only when you hit a real limit:

| Tool now | Switch to | When |
|---|---|---|
| Platform-native scheduler | n8n or Apache Airflow | You have 5+ jobs with dependencies between them (job B should only run after job A succeeds) |
| Railway/Render | Self-managed VM (Oracle/Hetzner) | You need more control over the environment, or costs grow past the free tier |
| Supabase hosted | Self-hosted Postgres+PostGIS | You need geospatial queries Supabase's free tier can't handle, or costs grow |
| Free NASA/Sentinel data | Licensed data (UP42, Planet) | A paying customer needs a specific AOI/resolution the free data can't give |
| Manual PDF/text insight | Templated report generator | You're producing insights often enough that formatting by hand is slow |

---

## Quick tool reference

| Purpose | Tool | Why this one, given your background |
|---|---|---|
| Code editor | VS Code | Standard, huge ecosystem of extensions |
| Containerization | Docker | The actual skill gap — makes code portable to any server |
| Version control | Git + GitHub | Needed the moment more than one machine (laptop + server) touches the code |
| Database | Supabase (hosted Postgres) | No server to manage, generous free tier, real SQL you already half-know |
| Hosting | Railway or Render | Deploy-from-GitHub simplicity, before you need full VM control |
| Scheduling | Platform-native cron (Railway/Render), or n8n later | You already know how to write the job logic — this just triggers it |
| AI summarization | Claude API | Simple API call, no self-hosted model needed |
| Dashboards | Metabase | Easiest open-source BI tool to learn |
| Website hosting | GitHub Pages / Cloudflare Pages | Free, connects directly to your repo |
| Satellite data (free) | earthaccess (NASA), Copernicus Data Space (Sentinel) | Official, well-documented, actively maintained |

---

*Reminder: keep ISRO/Bhuvan/MOSDAC data out of anything you plan to sell — it's free for research only, not resale (see earlier note). Everything in this plan uses NASA/Copernicus data, which is fully open for commercial use.*
