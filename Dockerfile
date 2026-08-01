FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend backend
COPY frontend frontend

# Persist the article database outside the container image.
ENV NEWS_DB_PATH=/data/news.db
RUN mkdir -p /data
VOLUME /data

EXPOSE 8000
# --no-proxy-headers: uvicorn will otherwise rewrite the client address from
# X-Forwarded-For as well, and Delphi's rate limiter reads that same header to
# decide who is calling. Two layers interpreting it means neither can be
# reasoned about — and uvicorn's reading changes with FORWARDED_ALLOW_IPS,
# which every "running on Fly" guide tells people to set to "*", at which point
# it takes the *first* entry of the header: the one the caller wrote. Turning
# it off leaves scope["client"] as the true socket peer, which is what
# backend/app/ratelimit.py counts back from.
CMD ["python", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", \
     "--port", "8000", "--no-proxy-headers"]
