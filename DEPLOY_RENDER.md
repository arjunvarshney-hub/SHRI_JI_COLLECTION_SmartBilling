# SHRI JI ENTERPRISES — Render deployment

This package is prepared for a Render Flask Web Service.

## Important database note
The app currently uses SQLite. Render's default filesystem is ephemeral, so **do not deploy this as-is for real shop data without persistent storage**. Attach a Render Persistent Disk and mount it at `/var/data`. The app then uses `/var/data/smartbilling.db` and copies the preserved bundled database there on first startup.

Render documents that persistent disks preserve filesystem changes, while the default filesystem does not. Persistent disks are available on paid web services.

## Render settings
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app`
- Environment variable: `SMARTSHOP_DATA_DIR=/var/data`
- Python: 3.13
- Persistent Disk mount path: `/var/data`

## Deploy
1. Put this folder in a GitHub repository.
2. In Render, create a Web Service from the repository.
3. Use the build/start commands above.
4. Attach a Persistent Disk mounted at `/var/data`.
5. Deploy.
6. Open the generated `https://...onrender.com` URL from any PC/phone.

## AI
Set `OPENAI_API_KEY` in Render Environment Variables if you want external AI responses. The built-in fallback remains available without it.
