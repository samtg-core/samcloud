#!/bin/sh
set -e

# Registry writes Caddyfile to the shared volume; use it if available
# Caddyfile lives on the shared volume so registry can append routes.
# Bootstrap writes the initial config; registry's caddy.py appends to it.
CADDYFILE="/registry-data/Caddyfile"

if [ -f "$CADDYFILE" ]; then
    echo "[caddy] Using existing Caddyfile from registry volume"
    exec caddy run --config "$CADDYFILE" --adapter caddyfile
fi

# First boot — generate bootstrap Caddyfile on the shared volume
DOMAIN="${CONSOLE_DOMAIN:-:443}"
echo "[caddy] Generating bootstrap Caddyfile for ${DOMAIN}"

# A site address with no host (`:443`) has no name to get a certificate for, and
# Caddy then fails every TLS handshake on it. Give it certificates from Caddy's
# own CA, issued for whatever name or address the client asks for (#1010).
case "$DOMAIN" in
    :*) NAMELESS=1 ;;
    *)  NAMELESS= ;;
esac

cat > "$CADDYFILE" << EOF
{
    admin 0.0.0.0:2019
    $([ -n "${ACME_EMAIL}" ] && echo "email ${ACME_EMAIL}")
    $([ -n "${ACME_CA}" ] && echo "acme_ca ${ACME_CA}")
    $([ -n "${NAMELESS}" ] && echo "default_sni samcloud.internal")
}

# TLS snippet — imported by all site blocks (registry-managed routes include this)
$([ -n "${NAMECHEAP_API_KEY}" ] && echo "(samcloud-tls) {
    tls {
        dns namecheap {
            api_key ${NAMECHEAP_API_KEY}
            user ${NAMECHEAP_API_USER}
        }
        resolvers 1.1.1.1 8.8.8.8
    }
}")

${DOMAIN} {
    $(if [ -n "${NAMELESS}" ]; then printf 'tls internal {\n        on_demand\n    }'; elif [ -n "${NAMECHEAP_API_KEY}" ]; then echo "import samcloud-tls"; fi)
    handle /api/v1/* {
        reverse_proxy http://registry:7777 {
            flush_interval -1
        }
    }
    handle {
        reverse_proxy http://console:3000
    }
}

$([ -n "${RUSTDESK_DOMAIN}" ] && cat <<RUSTDESK
${RUSTDESK_DOMAIN} {
    $([ -n "${NAMECHEAP_API_KEY}" ] && echo "import samcloud-tls")
    route {
        reverse_proxy /ws/id hbbs:21118 {
            transport http {
                read_timeout 120s
            }
        }
        reverse_proxy /ws/relay hbbr:21119 {
            transport http {
                read_timeout 120s
            }
        }
        respond "RustDesk server" 200
    }
}
RUSTDESK
)
EOF

echo "[caddy] Starting"
exec caddy run --config "$CADDYFILE" --adapter caddyfile
