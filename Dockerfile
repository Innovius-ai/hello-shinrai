FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HELLO_SHINRAI_BIND=0.0.0.0
WORKDIR /app

RUN useradd --create-home --uid 10002 shinrai
COPY pyproject.toml README.md LICENSE NOTICE requirements.lock ./
COPY src ./src
RUN python -m pip install --no-cache-dir -r requirements.lock && python -m pip install --no-cache-dir --no-deps .

USER 10002:10002
EXPOSE 8765
ENTRYPOINT ["hello-shinrai", "--no-browser"]
