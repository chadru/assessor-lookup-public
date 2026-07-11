"""Small outbound-URL guardrails shared by assessor clients."""

import ipaddress
import socket
import urllib.parse
import urllib.request


_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


def require_https_url(url, allowed_hosts=(), allowed_suffixes=(),
                      resolve_host=False):
    """Return *url* after enforcing the client's outbound trust boundary."""
    parsed = urllib.parse.urlsplit(str(url))
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or not host:
        raise ValueError("assessor endpoints must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("assessor endpoint URLs cannot contain credentials")
    if parsed.port not in (None, 443):
        raise ValueError("assessor endpoint URLs must use the default HTTPS port")
    if host in _LOCAL_HOSTNAMES or host.endswith(".localhost"):
        raise ValueError("assessor endpoints cannot target local hostnames")
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        try:
            literal = ipaddress.ip_address(socket.inet_aton(host))
        except OSError:
            literal = None
    if literal is not None and not literal.is_global:
        raise ValueError("assessor endpoints cannot target private addresses")

    hosts = {h.lower().rstrip(".") for h in allowed_hosts}
    suffixes = tuple(s.lower() for s in allowed_suffixes)
    if hosts or suffixes:
        if host not in hosts and not any(host.endswith(s) for s in suffixes):
            raise ValueError("assessor endpoint host is not permitted")
    if resolve_host:
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError("assessor endpoint host could not be resolved") from exc
        if not infos:
            raise ValueError("assessor endpoint host could not be resolved")
        for info in infos:
            address = info[4][0].split("%", 1)[0]
            try:
                resolved = ipaddress.ip_address(address)
            except ValueError as exc:
                raise ValueError("assessor endpoint resolved unexpectedly") from exc
            if not resolved.is_global:
                raise ValueError(
                    "assessor endpoint resolved to a private address"
                )
    return str(url)


class SameOriginHTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only when they stay on an approved public HTTPS host."""

    def __init__(self, allowed_hosts):
        super().__init__()
        self.allowed_hosts = tuple(allowed_hosts)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require_https_url(
            newurl, allowed_hosts=self.allowed_hosts, resolve_host=True,
        )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_https_opener(allowed_hosts, *handlers):
    """Build an opener whose redirects remain on the original host boundary."""
    return urllib.request.build_opener(
        *handlers, SameOriginHTTPSRedirectHandler(allowed_hosts),
    )


def open_https(request, timeout, allowed_hosts):
    """Open a validated public HTTPS request with same-origin redirects."""
    url = request.full_url if hasattr(request, "full_url") else str(request)
    require_https_url(url, allowed_hosts=allowed_hosts, resolve_host=True)
    return build_https_opener(allowed_hosts).open(request, timeout=timeout)
