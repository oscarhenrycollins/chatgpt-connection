# ChatGPT (Wizard AI connection app): a small stdlib Python page that drives the agent's own ChatGPT sign-in
# through the stock Hermes Agent dashboard API. No ChatGPT token ever enters this image or its process.
FROM python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 WIZARD_CHATGPT_PORT=8800
LABEL org.opencontainers.image.source="https://github.com/humanitylabs-org/wizard-app-store" \
      org.opencontainers.image.description="ChatGPT: connect a ChatGPT subscription once through the agent's own sign-in. Reports wizard.ai-connection/2 status." \
      org.opencontainers.image.licenses="MIT"
RUN mkdir -p /app/static
COPY --chmod=0444 server.py inference.py SKILL.md LICENSE /app/
COPY --chmod=0444 static/ /app/static/
USER 1000:1000
WORKDIR /app
EXPOSE 8800
CMD ["python3", "/app/server.py"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8800/health', timeout=3).read()"]
