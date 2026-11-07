# ClipAI Backend

ClipAI turns a long YouTube video into ready-to-post vertical Shorts. Paste a link, and the backend finds the best moments, cuts them into 9:16 clips with styled captions, and publishes them to YouTube Shorts and Facebook Reels.

## Features

- Downloads a YouTube video and transcribes it with Whisper (via Groq)
- Picks the most engaging moments using an LLM (via OpenRouter)
- Cuts clips and reformats them to 9:16 with a 4:3 video window, animated captions and a highlighted keyword
- Supports English, Hindi, Hinglish and Bengali
- Stores clips in Backblaze B2 and serves them through signed URLs
- Publishes to YouTube Shorts and Facebook Page Reels, individually or to all connected platforms in one click
- Google sign-in through Supabase, with a per-user clip limit

## How it works

1. `yt-dlp` downloads the video and extracts the audio.
2. Groq Whisper transcribes the audio.
3. An LLM selects viral moments and writes captions.
4. `ffmpeg` cuts each clip, applies the 9:16 layout and burns in captions.
5. Clips are uploaded to Backblaze B2 and recorded in Supabase.
6. On request, clips are published to YouTube and/or Facebook.

## Tech stack

FastAPI, yt-dlp, ffmpeg, Groq (Whisper), OpenRouter, Supabase (Auth and Postgres), Backblaze B2 (S3-compatible), YouTube Data API v3, Meta Graph API.

## Prerequisites

- Python 3.10 or newer (3.11+ recommended)
- ffmpeg available on your `PATH` (built with libass for captions)
- Node.js (used by yt-dlp to solve YouTube challenges)
- Accounts and keys for: Supabase, Groq, OpenRouter, Backblaze B2, Google Cloud
- Optional: a Meta developer app (for Facebook publishing)

## Configuration

Copy `.env.example` to `.env` and fill in the values.

| Variable | Required | Description |
|---|---|---|
| `GROQ_API_KEY` | Yes | Groq API key (transcription) |
| `OPENROUTER_KEY` | Yes | OpenRouter API key (moment selection and titles) |
| `SUPABASE_URL` | Yes | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Yes | Supabase `service_role` key (server-side only) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Yes | OAuth client for YouTube uploads |
| `B2_KEY_ID` / `B2_APP_KEY` | Yes | Backblaze B2 application key |
| `B2_BUCKET_NAME` | Yes | Private B2 bucket name |
| `B2_ENDPOINT` | Yes | B2 S3 endpoint, e.g. `https://s3.us-west-004.backblazeb2.com` |
| `BACKEND_URL` | Yes | Public URL of this backend (`http://localhost:8000` locally) |
| `FRONTEND_URL` | Yes | Public URL of the frontend (`http://localhost:3000` locally) |
| `YT_COOKIES` | Recommended | Contents of a Netscape `cookies.txt` (see below) |
| `FACEBOOK_APP_ID` / `FACEBOOK_APP_SECRET` | Optional | Enables Facebook Reels publishing |
| `ZOOM_FACTOR` | Optional | Extra zoom on the video window (`1.0` = none) |
| `BG_STYLE` | Optional | Background around the video: `dark` (default) or `blur` |

Never commit `.env` or `cookies.txt`. Both are listed in `.gitignore`.

## Database setup

In the Supabase SQL Editor, run `schema.sql`, then run the following if it is not already part of your schema:

```sql
create table if not exists public.fb_tokens (
  user_id uuid primary key,
  page_id text,
  page_name text,
  page_picture text,
  page_token text,
  updated_at timestamptz default now()
);
alter table public.fb_tokens enable row level security;
grant select, insert, update, delete on public.fb_tokens to service_role;

alter table public.clips
  add column if not exists uploaded_to jsonb not null default '[]'::jsonb;
```

Then, in Supabase, enable the Google provider under Authentication, Providers. Under Authentication, URL Configuration, set the Site URL to your frontend URL and add `http://localhost:3000` to the Redirect URLs.

## Run locally

```bash
git clone https://github.com/sweetkutub-svg/clipai-backend.git
cd clipai-backend

python -m venv venv
# Windows: venv\Scripts\activate
# macOS/Linux: source venv/bin/activate

pip install -r requirements.txt
cp .env.example .env        # then edit .env
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000/docs to confirm the API is running. The matching frontend runs on http://localhost:3000.

## Run with Docker

```bash
docker build -t clipai-backend .
docker run -p 7860:7860 --env-file .env clipai-backend
```

The container listens on `$PORT` (default `7860`).

## Deployment

The included `Dockerfile` works on any platform that runs Docker containers. Set every variable from the table above in the platform's environment settings, using the live URLs for `BACKEND_URL` and `FRONTEND_URL`.

Video encoding is CPU and memory intensive. Very small instances (for example 512 MB RAM or a fraction of a CPU) may be slow or run out of memory. For two or more users processing videos at the same time, at least 1 GB of RAM and 1 vCPU is recommended.

## Connecting platforms

### YouTube
1. In Google Cloud, enable **YouTube Data API v3** and create an OAuth client of type *Web application*.
2. Add this authorized redirect URI: `<BACKEND_URL>/auth/youtube/callback`.
3. While the consent screen is in *Testing* mode, add your Google account as a test user.

### Facebook (optional)
1. Create a Meta app and add the **Manage everything on your Page** use case.
2. Add the permissions `pages_show_list`, `pages_read_engagement` and `pages_manage_posts`.
3. Add this valid OAuth redirect URI: `<BACKEND_URL>/auth/facebook/callback`.
4. Facebook publishing targets a Facebook **Page**, not a personal profile. Reels must be 3 to 60 seconds long, so use the 30–60 s clip length.
5. While the app is in development mode, only app admins and testers can connect.

## YouTube download cookies

YouTube often blocks downloads from data center IP addresses. Export a Netscape `cookies.txt` from a **throwaway Google account** (never your main account) using a private window and a cookie export extension, and provide it in one of two ways:

- Locally: save it as `cookies.txt` next to `main.py`.
- On a host: paste its contents into the `YT_COOKIES` environment variable.

Refresh the cookies if downloads start failing with a "Sign in to confirm you're not a bot" error.

## Captions and fonts

Captions are rendered with libass using the fonts in `fonts/` (Poppins ExtraBold for Latin and Devanagari, Hind Siliguri Bold for Bengali). Both fonts are licensed under the SIL Open Font License.

## Troubleshooting

| Problem | Fix |
|---|---|
| `ffmpeg not found` | Install ffmpeg and restart the terminal |
| "Sign in to confirm you're not a bot" | Export fresh YouTube cookies |
| Login redirects back with an error | Check the redirect URLs in Google Cloud, Meta and Supabase |
| Facebook upload rejected | Make sure the clip is 60 seconds or shorter and the Page is connected |
| `permission denied for table` in Supabase | Run the SQL in "Database setup", including the `grant` line |

## Disclaimer

This project is intended for processing content you own or have permission to use. You are responsible for complying with YouTube's Terms of Service, Meta's platform terms and applicable copyright law.

## License

MIT License. See the [LICENSE](LICENSE) file.

## Author

SK Kutubuddin
Portfolio: https://codewithkutub.online
Email: codewithkutub@gmail.com
