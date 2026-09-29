FROM python:3.13-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
# Set as an image ENV, not just a build-arg for our own RUN steps below,
# because it needs to persist into the *running* container: Harbor's own
# agent installer later runs its own `apt-get install curl bash nodejs npm
# procps` inside this container (after it's already up) to bootstrap the
# Claude Code CLI. nodejs pulls in tzdata as a transitive dependency, whose
# postinst prompts interactively for a timezone with no TTY present --
# without this, that prompt hangs indefinitely instead of erroring, and
# the agent's setup step times out (AgentSetupTimeoutError) well before
# the agent ever gets a chance to look at the task. None of our own
# build-time installs below happen to pull in tzdata, which is why this
# went unnoticed until Harbor's post-build install step hit it.

RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql \
        postgresql-contrib \
        sudo \
        curl \
        procps \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "psycopg[binary]==3.2.9"

WORKDIR /app
COPY postgres /app/postgres
COPY src /app/src
COPY runbooks /app/runbooks
COPY telemetry /app/telemetry

RUN mkdir -p /app/telemetry /app/remediation /app/src \
    && chmod +x /app/src/start.sh /app/src/entrypoint.sh /app/src/*.py \
    && python3 /app/src/gen_snapshot.py \
    && rm -f /app/src/gen_snapshot.py \
    && chmod -R a-w /app/postgres
# gen_snapshot.py is the tool used once, at build time, to freeze the first
# minute of the incident into /app/telemetry/incident_window -- it is not
# part of the live request path and no running service imports it. It is
# removed from the image right after it runs so the shipped container holds
# only the frozen output (traces.jsonl/services.jsonl/metrics.json/
# postgres_slow.log) an on-call engineer would actually have, not the
# generator whose source names which synthetic span dominates each
# request's latency and which log lines are cosmetic -- keeping that script
# on disk would hand out the incident's answer key instead of requiring it
# to be read off the live signals the way the rest of this task intends.
#
# /app/postgres holds the schema/seed source used once at image build time to
# populate the running database -- nothing at runtime writes to it (start.sh
# only touches /app/telemetry's permissions, for postgres's own live log
# output). It's made read-only here as defense in depth, but the actual
# guarantee is structural, not this permission bit: grading never reads this
# container's filesystem at all. It rebuilds a fresh database from its own
# sealed copy of schema.sql/seed.sql inside the separately-built verifier
# image, and scores solely on /app/remediation/migration.sql applied there.
# Editing seed data, telemetry, service source, or anything under /logs in
# this container cannot influence grading.

ENV PLATFORM_LOG_DIR=/app/telemetry
ENV DATABASE_URL=postgresql://checkout:checkout@127.0.0.1:5432/checkout
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080 8081 8082 8083 5432

ENTRYPOINT ["/bin/bash", "/app/src/entrypoint.sh"]
CMD ["sleep", "infinity"]
