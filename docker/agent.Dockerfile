# The agent: pure Python. No LibreOffice, no poppler, no native anything.
#
# That is the point of the single-process design: everything the agent does to a deck - probe,
# compile, apply, byte-preserving save, overflow and overlap checks - is python-pptx and lxml.
# The heavy native toolchain lives in the sandbox image and is only reached for rendering and
# for running code a model wrote.
FROM python:3.12-slim-bookworm

# The docker CLI only, so the agent can start sandbox containers. It talks to a daemon it is
# given; it does not run one. (See the warning in compose.yml about which daemon.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io git ca-certificates \
    && apt-get clean \
    && find /var/lib/apt/lists -type f -delete

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/
COPY engine/ /app/engine/
COPY runner/ /app/runner/
COPY config/ /app/config/

ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "-m", "deck_agent.cli"]
CMD ["doctor"]
