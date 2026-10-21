# Submission image for the agent track (CTFY-0001 §4).
# NOTE: competition runs OFFLINE (internet_policy=deny) -> bake every dependency in here.
#       Do NOT fetch anything at run time.
FROM python:3.12-slim

# --- system toolchain (bake in; no network at run time) ---
# TODO(team): pin a full toolchain. Keep it lean to respect image-size limits.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl openssl ca-certificates git \
        gdb \
        chromium \
    && rm -rf /var/lib/apt/lists/*

# --- python deps + package ---
WORKDIR /app
COPY pyproject.toml /app/
COPY agent /app/agent
RUN pip install --no-cache-dir -e ".[model,mcp]"
# TODO(team): add the CTF toolchain the solver shells out to, e.g. pwntools etc.

# IMPORTANT (CTFY-0001 §1.2): the platform sets cwd=/ctfy/work, so the entrypoint
# must NOT resolve a relative path. Invoke the module by name.
ENTRYPOINT ["python", "-m", "agent.entrypoints.image_main"]
