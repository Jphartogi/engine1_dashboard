FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

# Single worker: auth tokens are held in-memory (TOKENS dict in app.py), so a
# multi-worker setup would scatter sessions across processes and cause random 401s.
EXPOSE 8000
CMD ["gunicorn", "app:app", "-w", "1", "-b", "0.0.0.0:8000"]
