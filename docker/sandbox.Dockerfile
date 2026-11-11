# The sandbox image: the only place in this project where a native binary runs.
#
#  * model-written python-pptx code (a trust boundary)
#  * LibreOffice + poppler rendering, and the metric-compatible fonts the overflow
#    predictor measures with (a dependency boundary)
#
# There is deliberately no model client, no AWS SDK and no HTTP library in here, and
# containers are always started with --network none. Nothing in this image can phone home.
FROM python:3.12-slim-bookworm

# libreoffice-impress: pptx -> pdf.      poppler-utils: pdf -> png.
# carlito/caladea/liberation: metric-compatible substitutes for Calibri/Cambria/Arial, so
# text measurement matches what PowerPoint would do.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-impress \
        libreoffice-core \
        poppler-utils \
        fonts-crosextra-carlito \
        fonts-crosextra-caladea \
        fonts-liberation \
        fonts-dejavu-core \
        ca-certificates \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

COPY runner/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY runner/ /opt/runner/
RUN chmod -R a+rX /opt/runner

# Containers run as the *host* user's uid:gid (see sandbox/docker.py), which will not exist in
# /etc/passwd. Everything these scripts need is either world-readable or inside /work.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /work
# No ENTRYPOINT on purpose: the caller always passes a full, fixed argv.
CMD ["python3", "/opt/runner/selftest.py", "out"]
