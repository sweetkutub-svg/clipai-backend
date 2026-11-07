# ClipAI Backend (FastAPI)

## 1. One-time setup
1. **Supabase** → SQL Editor → run `schema.sql`. Authentication → Providers → enable Google.
   Authentication → URL Configuration → Site URL = your Vercel URL (add `http://localhost:3000` to Redirect URLs too).
2. **Google Cloud** (YouTube Data API v3 enabled) → OAuth client (Web). Authorized redirect URI:
   `https://<YOUR-BACKEND-URL>/auth/youtube/callback`
3. **Backblaze B2** bucket (private) + application key.
4. Keys: Groq, OpenRouter.

## 2. Free hosting — Hugging Face Spaces (recommended: 2 vCPU / 16 GB RAM free)
1. huggingface.co → New Space → SDK **Docker** → Blank → Public.
2. Upload all files of this folder (or `git push` to the Space repo).
3. Space → Settings → **Variables and secrets**: add every key from `.env.example` as a *Secret*
   (`BACKEND_URL` = `https://<user>-<space>.hf.space`, `FRONTEND_URL` = your Vercel URL).
4. Backend URL = `https://<user>-<space>.hf.space`. Put the same in Google redirect URI + Vercel `REACT_APP_API_URL`.
Note: free Spaces sleep after ~48h of no traffic (wakes on first request).

Alternative: Render free web service (Docker). Only 512 MB RAM / slow CPU, 15-min idle sleep — works but slower.

## 3. YouTube download note
YouTube often blocks datacenter IPs. Set `YT_COOKIES` secret with the content of a Netscape `cookies.txt`
exported from a **throw-away Google account** (never your main one). Refresh it if downloads start failing.

## Local run (apne computer par chalane ka tarika)

### Zaroori cheezein
- **Python 3.10+** (3.11 best)
- **ffmpeg** (terminal me `ffmpeg -version` chalna chahiye)
  - Windows: `winget install ffmpeg` (ya https://ffmpeg.org se download karke PATH me daal)
  - Mac: `brew install ffmpeg`
  - Linux: `sudo apt install ffmpeg fonts-dejavu-core`
- **Node.js** (yt-dlp YouTube ke liye use karta hai) + frontend ke liye bhi chahiye

### Backend
```bash
cd clipai-backend-main
python -m venv venv
# Windows:  venv\Scripts\activate
# Mac/Linux: source venv/bin/activate
pip install -r requirements.txt

# .env file already bani hai - khali keys (GROQ_API_KEY, OPENROUTER_KEY,
# SUPABASE_SERVICE_KEY, GOOGLE_CLIENT_ID/SECRET, B2_KEY_ID/B2_APP_KEY) bhar le
uvicorn main:app --reload --port 8000
```
Check: browser me http://localhost:8000/docs khulna chahiye.

### Frontend (alag terminal me)
```bash
cd clipai-frontend-main
npm install
npm start          # http://localhost:3000
```
Frontend ki `.env` me `REACT_APP_API_URL=http://localhost:8000` hai.

### Local ke liye ek baar ye settings check kar le
1. **Google Cloud -> OAuth client -> Authorized redirect URIs** me ye add kar:
   `http://localhost:8000/auth/youtube/callback`
2. **Supabase -> Authentication -> URL Configuration -> Redirect URLs** me `http://localhost:3000` add kar.
3. `.env` me `BACKEND_URL=http://localhost:8000` aur `FRONTEND_URL=http://localhost:3000` rehna chahiye.
4. `.env` badalne ke baad backend dobara start kar (frontend ke liye `npm start` dobara).

### Video zoom
16:9 video ko 9:16 shorts me convert karne ke baad main video thora zoom-in hota hai.
Kam/zyada karna ho to `.env` me `ZOOM_FACTOR` badal (`1.0` = zoom band, `1.15` = default, `1.3` = zyada).

### Dikkat aaye to
- `ffmpeg not found` -> ffmpeg install karke terminal restart kar.
- YouTube download fail -> `cookies.txt` purani ho gayi hogi, nayi export karke backend folder me replace kar.
- Login ke baad wapas nahi aa raha -> upar wale redirect URL (Google + Supabase) dubara check kar.
- `.env` aur `cookies.txt` kabhi GitHub par push mat karna (`.gitignore` me already hain).
