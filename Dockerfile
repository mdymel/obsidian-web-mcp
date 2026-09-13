FROM python:3.14.7-alpine3.24
RUN apk add --no-cache git ripgrep
WORKDIR /app
COPY pyproject.toml README.md LICENSE /app/
COPY src/ /app/src/
RUN pip install --no-cache-dir .
RUN python -c "from obsidian_vault_mcp.server import main; assert callable(main)"
EXPOSE 8420
CMD ["vault-mcp"]
