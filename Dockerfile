# DataLens AI - public deployment image (works on Hugging Face Spaces, or any Docker host).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --user -r requirements.txt

COPY --chown=user . .

# Build the cricket database at image build time (about 5 minutes, downloads ~35 Cricsheet archives).
# Change DATA_VERSION to force a rebuild with fresh data.
ARG DATA_VERSION=1
RUN echo "data version: $DATA_VERSION" && python -m datalens.loader

# Public mode: private per-visitor sessions, rate limits, upload caps, security headers.
ENV DATALENS_PUBLIC=1 HOST=0.0.0.0 PORT=7860
EXPOSE 7860
CMD ["python", "-m", "datalens.api"]
