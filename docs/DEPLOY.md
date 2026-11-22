# Deploying DataLens AI publicly (Hugging Face Spaces)

A Space is a free, always-available web host for apps. This project ships a `Dockerfile` that builds the cricket
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

1. Create a free account at https://huggingface.co.
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
- **Hardware:** the free CPU Space (2 vCPU, 16 GB RAM) is enough. Free Spaces sleep after about 2 days without visitors
  and wake on the next visit.

## Before you share the link
- Check the app works: open the link, run a ranking, try the Auction tab, upload a small CSV.
- Don't add secrets to the Space unless you need them. If you set `ANTHROPIC_API_KEY` **and** `DATALENS_ALLOW_LLM=1`,
  every visitor can trigger paid AI calls (rate limits still apply), so only do that deliberately.
- Auction prices are synthetic examples. Say so (the app already does) and don't present them as real.

## Running public mode on your own machine (to test)
```bash
DATALENS_PUBLIC=1 PORT=8002 python -m datalens.api
```
