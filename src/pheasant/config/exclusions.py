"""Default filesystem exclusion patterns shared by source configurations."""

# Secret patterns are enforced by the effective-source policy even when a
# caller replaces the ordinary exclude list. Keep them distinct from noise.
SECRET_EXCLUDES = [
    # Environment and dotenv files
    "**/.env",
    "**/.env.*",
    "**/*.envrc",
    # Private keys and certificates
    "**/*id_rsa*",
    "**/*id_dsa*",
    "**/*id_ecdsa*",
    "**/*id_ed25519*",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/*.jks",
    "**/*.keystore",
    "**/*.asc",
    "**/*.gpg",
    # Credential stores people keep in a home directory
    "**/.ssh/**",
    "**/.gnupg/**",
    "**/.aws/**",
    "**/.azure/**",
    "**/.kube/**",
    "**/.docker/config.json",
    "**/.config/gh/**",
    "**/.config/gcloud/**",
    "**/.netrc",
    "**/.npmrc",
    "**/.pypirc",
    "**/.git-credentials",
    "**/credentials",
    "**/credentials.json",
    "**/secrets.yaml",
    "**/secrets.yml",
    "**/*.kdbx",
    # Local keychains / browser profiles
    "**/Library/Keychains/**",
    "**/.mozilla/**",
    "**/.password-store/**",
]

# Generated directories are about indexing cost, not disclosure. Unlike the
# secret list, operators may choose to include these in a source.
NOISE_EXCLUDES = [
    "**/.git/**",
    "**/node_modules/**",
    "**/__pycache__/**",
    "**/.venv/**",
    "**/venv/**",
    "**/dist/**",
    "**/build/**",
    "**/target/**",
    "**/.next/**",
    "**/.cache/**",
    "**/.tox/**",
    "**/.gradle/**",
    "**/.terraform/**",
    "**/.mypy_cache/**",
    "**/.pytest_cache/**",
    "**/.ruff_cache/**",
    # Generated or machine-written text that the broader default includes
    # would otherwise pick up: bundles, source maps, resolved dependency pins.
    "**/*.min.js",
    "**/*.min.css",
    "**/*.map",
    "**/package-lock.json",
    "**/pnpm-lock.yaml",
    "**/yarn.lock",
    "**/poetry.lock",
    "**/uv.lock",
    "**/Cargo.lock",
    "**/Gemfile.lock",
    "**/composer.lock",
    "**/go.sum",
]

DEFAULT_EXCLUDES = [*NOISE_EXCLUDES, *SECRET_EXCLUDES]
