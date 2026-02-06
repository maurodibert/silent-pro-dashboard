# Silent Pro Dashboard

Amazon FBA sales dashboard for Silent Pro basketball products.

## Features

- Real-time sales data from Amazon SP-API
- Filter by product and date range
- Argentina timezone support (5am-5am business day)
- Mobile-first responsive design

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000

## Deploy to Railway

1. Push to GitHub
2. Go to [railway.app](https://railway.app)
3. New Project → Deploy from GitHub repo
4. Add environment variables from `.env`
5. Deploy!

## Deploy to Render

1. Push to GitHub
2. Go to [render.com](https://render.com)
3. New Web Service → Connect GitHub repo
4. Build command: `pip install -r requirements.txt`
5. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
6. Add environment variables
7. Deploy!

## Deploy to Vercel

Create `vercel.json`:
```json
{
  "builds": [{"src": "main.py", "use": "@vercel/python"}],
  "routes": [{"src": "/(.*)", "dest": "main.py"}]
}
```

## Environment Variables

See `.env.example` for required variables.
