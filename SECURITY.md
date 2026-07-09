# Security policy

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability or accidental
data exposure. Use the repository's **Security** tab to submit a private
security advisory. Include affected versions, reproduction steps, impact, and
any suggested mitigation. Please avoid including real MLS exports or private
property data unless it is necessary to reproduce the issue.

## Supported releases

Security fixes are applied to the latest published release. Older releases may
be asked to upgrade before receiving a backport.

## MCP trust boundary

`assessor-lookup-mcp` is designed for a trusted local client over stdio. It is
not an authenticated HTTP, SSE, or multi-user service and must not be exposed
through an unauthenticated network bridge.

The CSV tool confines resolved `.csv` paths to the process working directory,
or to `ASSESSOR_LOOKUP_MCP_DATA_DIR` when that directory is explicitly set.
Outbound assessor requests require HTTPS, resolve only to public addresses,
and follow redirects only on the original approved host.

## Data handling

Packaged live regression fixtures use government or institutional properties
and include only parser-critical stable fields. User-onboarded cases and MLS
exports remain in the user's local configuration or chosen data directory and
must not be committed to the repository. On POSIX systems, onboarding enforces
mode `0700` on its configuration directory and `0600` on property-data files,
including remediation of existing files.
