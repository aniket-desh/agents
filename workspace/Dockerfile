FROM node:22-bookworm-slim

ARG CODEX_VERSION=0.158.0
ARG CLAUDE_VERSION=2.1.205
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git openssh-client python3 python3-venv rsync tmux \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global --prefix /opt/vendor \
      "@openai/codex@${CODEX_VERSION}" "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
    && npm cache clean --force

ARG USER_UID=1000
ARG USER_GID=1000
RUN test "$USER_UID" -gt 0 && test "$USER_GID" -gt 0 \
    && userdel node \
    && if getent group node >/dev/null; then groupdel node; fi \
    && if ! getent group "$USER_GID" >/dev/null; then groupadd --gid "$USER_GID" research; fi \
    && useradd --uid "$USER_UID" --gid "$USER_GID" --create-home \
      --home-dir /home/research --shell /bin/bash research \
    && mkdir /workspace \
    && touch /home/research/.research-home-layout /workspace/.research-workspace-layout \
    && chown -R "$USER_UID:$USER_GID" /home/research /workspace \
    && chmod 0700 /home/research /workspace

WORKDIR /opt/research-agents
COPY install.sh ./
COPY scripts/install.py ./scripts/install.py
COPY scripts/research-workspace.py ./scripts/research-workspace.py
COPY research_runtime/ ./research_runtime/
COPY bin/research-compute ./bin/research-compute
COPY guidance/ ./guidance/
COPY integrations/ ./integrations/
COPY skills/ ./skills/
COPY docs/ ./docs/
COPY workspace/ ./workspace/
RUN printf 'research-agents-isolated-v1\n' > /etc/research-workspace \
    && chmod 0444 /etc/research-workspace \
    && chmod 0755 install.sh bin/research-compute workspace/entrypoint.sh workspace/bin/codex workspace/bin/claude \
    && ln -s /opt/research-agents/workspace/bin/codex /usr/local/bin/codex \
    && ln -s /opt/research-agents/workspace/bin/claude /usr/local/bin/claude \
    && ln -s /home/research/.local/bin/research-compute /usr/local/bin/research-compute \
    && printf 'export PATH="/opt/research-agents/workspace/bin:$HOME/.local/bin:$PATH"\n' > /etc/profile.d/research.sh \
    && chmod -R go-w /opt/research-agents

LABEL org.research-agents.workspace="v1"
ENV HOME=/home/research \
    PATH=/opt/research-agents/workspace/bin:/home/research/.local/bin:/opt/vendor/bin:/usr/local/bin:/usr/bin:/bin \
    RESEARCH_COMPUTE_SOCKET=/run/research-compute/control.sock \
    PYTHONDONTWRITEBYTECODE=1
USER ${USER_UID}:${USER_GID}
WORKDIR /workspace
ENTRYPOINT ["/opt/research-agents/workspace/entrypoint.sh"]
CMD ["sleep", "infinity"]
