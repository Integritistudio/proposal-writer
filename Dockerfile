# Upwork Proposal Agent — run with Docker or on Render/Railway/Fly
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create learning dir so logs/ratings can be written (override with volume if needed)
RUN mkdir -p learning

ENV PORT=5001
EXPOSE 5001

CMD ["python", "app.py"]
