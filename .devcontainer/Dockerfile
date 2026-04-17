FROM mcr.microsoft.com/devcontainers/javascript-node:20

# Install additional tools
RUN apt-get update && apt-get install -y \
    git \
    curl \
    vim \
    htop \
    && rm -rf /var/lib/apt/lists/*

# Install global npm packages
RUN npm install -g \
    npm@latest \
    pnpm \
    @anthropic-ai/claude-code

# Set working directory
WORKDIR /workspace

# Create node_modules directory with correct permissions
RUN mkdir -p /workspace/frontend/node_modules && \
    chown -R node:node /workspace

USER node

# Default command
CMD ["sleep", "infinity"]
