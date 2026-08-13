# Security policy

## Security boundaries

- **Project scan exclusions:** scanning is limited to an active direct project
  root and excludes private state, aliases, and unsafe paths.
- **Update validation:** public hotword updates require HTTPS, checksum, and
  signature validation before they replace local data.
- **Hook trust:** host hooks invoke the installed, owned skill path and fail
  open if the local normalization contract is unavailable or invalid.
- **Permissions:** adapters use only documented platform capabilities. They do
  not inspect undocumented host files or delete content without verified
  ownership.
- **Release privacy:** runtime archives use exact allowlists and exclude
  personal lexicons, tokens, test fixtures, caches, build outputs, and local
  absolute paths.

## Vulnerability report

Please submit a private vulnerability report to the project maintainer with a
minimal reproduction, affected version, impact, and any relevant logs with
secrets removed. Do not publish exploit details until a maintainer has had a
reasonable opportunity to investigate and release a fix.
