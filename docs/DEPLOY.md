# Deploying DataLens AI publicly (Hugging Face Spaces)

> **Cost note (checked Nov 2026):** Hugging Face now requires a **PRO subscription** to run a Docker Space on the
> free CPU tier (creating one without PRO fails with `402 Payment Required`). Static Spaces are free but cannot run
> this Python app. If you don't want to pay, use the free temporary tunnel in the section at the end, or another
> host that can run a Docker image (for example Google Cloud Run, which needs a billing card).

A Space is an always-available web host for apps. This project ships a `Dockerfile` that builds the cricket
database during the build and starts the app in **public mode**.

## What public mode does
Turned on by `DATALENS_PUBLIC=1` (the Dockerfile sets it):

| Protection | Behaviour |
|---|---|
| Private sessions | Each browser gets a random id (`X-Session-Id`). Uploads, saved projects, roster lists and price lists are separate per visitor, kept in memory / a temp folder, and deleted after 6 hours idle. Max 300 live sessions. |
| Upload caps | 25 MB per file, 3 uploaded tables per visitor, 2 MB for price/roster files; files are read in chunks and refused as soon as they exceed the cap. |
| Rate limits | Per client address: 150 requests/minute overall, 30/minute for heavy actions (planner, similarity, chat, uploads). Over the limit returns HTTP 429. Tune with `DATALENS_RATE_GENERAL` / `DATALENS_RATE_HEAVY`. |
| No raw SQL | `/api/sql` is disabled; API docs are hidden. Uploaded-table SQL stays on each visitor's own small table. |
| No paid AI by default | Claude chat is off (the free rule-based chat runs) even if an API key is set, unless `DATALENS_ALLOW_LLM=1`. |
| Headers | `nosniff`, no-referrer, a Content-Security-Policy, and no-store caching for API responses. |
| Errors | Unexpected server errors return a generic message instead of internal details. |
| Credit | The footer credits Cricsheet and states the Open Data Commons Attribution License. |

## One-time setup (about 10 minutes plus the first build)

1. Create an account at https://huggingface.co and subscribe to PRO (needed for Docker Spaces, see the cost note above).
2. Create an access token with **write** permission: Settings -> Access Tokens -> New token. Keep it private.
3. In a terminal, from the project folder (with your virtual environment active):

```bash
pip install -U huggingface_hub
hf auth login          # paste your token when asked
```

4. Bundle the files a Space needs:

```bash
bash scripts/make_space_bundle.sh
```

5. Create the Space and upload the bundle (replace `YOUR_USERNAME`):

```bash
hf repo create YOUR_USERNAME/datalens-ai --repo-type space --space-sdk docker
hf upload YOUR_USERNAME/datalens-ai build/space . --repo-type space
```

6. Open https://huggingface.co/spaces/YOUR_USERNAME/datalens-ai and watch the **Logs**. The first build takes about
   10-15 minutes (it downloads the cricket data and builds the database). When it says *Running*, the public link is
   `https://YOUR_USERNAME-datalens-ai.hf.space`.

If a command name differs on your version, `huggingface-cli` is the older name for `hf` and takes the same arguments.

## Keeping it up to date
- **New matches:** open the Space -> Settings -> *Factory rebuild* (or change `ARG DATA_VERSION` in the Dockerfile and
  upload again). The build re-downloads Cricsheet data.
- **New code:** run `bash scripts/make_space_bundle.sh` and `hf upload ...` again.
- **Hardware:** the basic CPU Space has enough memory for the database. Spaces can sleep when idle and wake on the
  next visit.

## Before you share the link
- Check the app works: open the link, run a ranking, try the Auction tab, upload a small CSV.
- Don't add secrets to the Space unless you need them. If you set `ANTHROPIC_API_KEY` **and** `DATALENS_ALLOW_LLM=1`,
  every visitor can trigger paid AI calls (rate limits still apply), so only do that deliberately.
- Auction prices are synthetic examples. Say so (the app already does) and don't present them as real.

## Running public mode on your own machine (to test)
```bash
DATALENS_PUBLIC=1 PORT=8002 python -m datalens.api
```

## Free temporary public link (no account, no card)
Always run in **public mode** (never expose the normal local mode: it allows raw SQL and shared saves). In one terminal:

```bash
DATALENS_PUBLIC=1 PORT=8010 python -m datalens.api
```

In a second terminal open a tunnel; it prints an `https://...lhr.life` address:

```bash
ssh -R 80:localhost:8010 nokey@localhost.run
```

(Alternative: `brew install cloudflared`, then `cloudflared tunnel --url http://localhost:8010`.) The link only works while both
terminals are running and the computer is awake, and it changes each time.
