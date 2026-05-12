# Web/worker image for the online judge.
# The worker runs Docker containers through the host Docker socket, so the image
# must include the Docker CLI. Copying it from docker:cli is more reliable than
# installing docker.io with apt on slim images.
FROM docker:27-cli AS docker-cli

FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
