FROM python:3.13-alpine AS base

WORKDIR /app

RUN apk add --no-cache bubblewrap

COPY requirements.txt /app/
RUN python3.13 -m venv /app/venv && /app/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY src /app/src
RUN /app/venv/bin/python src/manage.py collectstatic --noinput


FROM python:3.13-alpine

ARG PORT=8000
ARG VERSION
ENV PORT ${PORT}
ENV VERSION ${VERSION}
ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN apk add --no-cache bubblewrap

COPY --from=base /app/venv/ /app/venv/
COPY src /app/src
COPY gunicorn.conf.py /app/gunicorn.conf.py
COPY --from=base /app/src/staticfiles/ /app/src/staticfiles/

CMD ["sh", "-c", "/app/venv/bin/gunicorn --config /app/gunicorn.conf.py --chdir src --bind 0.0.0.0:${PORT} main.wsgi"]
EXPOSE ${PORT}
