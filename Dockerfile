FROM python:3.14.7-alpine3.24
RUN apk add --no-cache git ripgrep
WORKDIR /app
COPY pyproject.toml README.md LICENSE /app/
COPY src/ /app/src/
RUN pip install --no-cache-dir .
RUN python -c "from obsidian_vault_mcp.video_frames import main; assert callable(main)"
EXPOSE 8420
# The stock server plus vault_save_video_frame, which stays off unless TRANSCRIPTOR_MCP_URL is set.
CMD ["vault-mcp-frames"]
