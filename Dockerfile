FROM rangermix/twitch-drops-miner:2.6.0@sha256:47dac0948845d436dacc843a7f2eeffb14cedb2eb69e54bdf648a2be19513f99 AS miner
RUN python -c "import importlib.metadata as m; print('\n'.join(sorted(d.metadata['Name']+'=='+d.version for d in m.distributions() if d.metadata['Name'].lower() not in {'twitch-drops-miner','pip','setuptools'})))" > /requirements-miner.txt

FROM python:3.15-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
RUN test "$(dpkg --print-architecture)" = amd64
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg tzdata xvfb xauth openbox x11vnc xdotool fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://dl.google.com/linux/linux_signing_key.pub -o /tmp/google-signing-key.pub \
    && gpg --batch --dearmor -o /usr/share/keyrings/google-chrome.gpg /tmp/google-signing-key.pub \
    && echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main' > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/* /tmp/google-signing-key.pub
RUN groupadd --gid 10001 tdm-browser \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin tdm-browser
COPY --from=miner /usr/share/novnc /usr/share/novnc
COPY --from=miner /requirements-miner.txt /requirements-miner.txt
COPY manager/requirements.txt /requirements-manager.txt
RUN python -m pip install --no-cache-dir -r /requirements-miner.txt -r /requirements-manager.txt \
    && python -m pip uninstall -y pip
RUN apt-get update && apt-get install -y --no-install-recommends wmctrl \
    && rm -rf /var/lib/apt/lists/*
# Keep dependency layers cached while building the engine from this fork.
COPY main.py /app/main.py
COPY src/ /app/src/
COPY web/ /app/web/
COPY lang/ /app/lang/
COPY icons/ /app/icons/
COPY LICENSE /app/LICENSE
COPY manager /manager
COPY THIRD_PARTY_NOTICES.md /app/THIRD_PARTY_NOTICES.md
RUN python /manager/adapt_upstream.py && mkdir -p /run/secrets /data /runtime && chmod 700 /run/secrets /data /runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TDM_BROWSER_COMPAT=off
LABEL org.opencontainers.image.title="Twitch Drops Manager" \
      org.opencontainers.image.source="https://github.com/gearheart89/twitch-drops-manager" \
      org.opencontainers.image.description="Multi-account Twitch Drops dashboard with encrypted storage and Unraid deployment" \
      net.unraid.docker.icon="https://raw.githubusercontent.com/gearheart89/twitch-drops-manager/manager/manager/web/icon.png"
WORKDIR /manager
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=30s --start-period=60s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/_manager/health', timeout=10)"
CMD ["python", "/manager/server.py"]
