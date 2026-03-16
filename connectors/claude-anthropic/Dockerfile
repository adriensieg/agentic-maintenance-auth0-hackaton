FROM python:3.11-slim

# Prevent Python buffering/log loss
ENV PYTHONUNBUFFERED=1

# Set working directory
WORKDIR /app

# Install dependencies first (better layer caching)
COPY app/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app ./app

# Expose FastAPI port
EXPOSE 8000

# Start FastAPI
CMD ["uvicorn", "app.app:app", "--host", "0.0.0.0", "--port", "8000"]
