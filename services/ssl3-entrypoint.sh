#!/bin/sh
# Build a small CA and a server cert signed by it, then hand off to stunnel.
#
# WHY A CA AND NOT A SELF-SIGNED CERT
#
# The Viewer ships its own trust store -- usr/all/url/cert.db -- holding four
# 1990s CA roots (VeriSign Class 1/2/3 and RSA Data Security Secure Server) and
# nothing else. A self-signed server cert chains to none of them, so the client
# completes the handshake and then resets with zero application bytes, which it
# reports as POL-1331. stunnel logs it as:
#
#     SSL_accept: Success (0)
#     Connection reset: 0 byte(s) sent to TLS, 0 byte(s) sent to socket
#
# So we mint our own CA, sign the server cert with it, and add the CA root to
# the client's store with tools/certdb.py. Nothing is patched in the binary.
#
# ERA-APPROPRIATE CRYPTO, deliberately
#
#   RSA 1024  -- the roots in the client's store are 1024-bit; a 2002 stack may
#                well have a ceiling, and there is nothing to gain by testing it
#   SHA-1     -- those roots are signed with MD2/MD5. SHA-1 is the newest digest
#                a client of this vintage can be relied on to verify; SHA-256
#                certificates are the likeliest way to fail this a second time
#
# The key must be RSA regardless: every cipher the Viewer offers uses RSA key
# exchange, so an ECDSA cert would leave no shared cipher at all.
#
# ---------------------------------------------------------------------------
# THIS CA IS NOT A SECRET AND NOT A TRUST ANCHOR FOR ANYTHING REAL. Its private
# key sits unencrypted in a docker volume. It exists so one 2003 game client on
# a private network will talk to one server. Do not install it anywhere else.
# ---------------------------------------------------------------------------
set -eu

SSL=/opt/ssl3/bin/openssl
CERT=/certs/ssl3.pem
CA_KEY=/certs/pol-ca.key
CA_CRT=/certs/pol-ca.pem
CN="${SSL3_CN:-*.pol.com}"

# VALIDITY MUST END BEFORE 19 JANUARY 2038.
#
# The first attempt used 7300 days, expiring in 2046, and the client answered
# POL-1328 -- a DIFFERENT error from the POL-1331 it gave when the CA was
# genuinely unknown, i.e. it had found and accepted our root and then rejected
# the certificate on something else. 2046 is past the signed 32-bit time_t
# cliff, so a client of this vintage parses that notAfter into a negative time
# and sees a certificate that expired in 1902. Every root already in the store
# expires in 2004, 2010 or 2020 -- comfortably inside the window.
#
# 4000 days from now lands in 2037. Do not raise this past 2038 to make the
# certificate "last longer"; it would last until the next person debugs it.
DAYS="${SSL3_DAYS:-4000}"

mkdir -p /certs

# The from-source OpenSSL was configured with --openssldir=/opt/ssl3 but no
# openssl.cnf was installed there, and `req` refuses to run without one:
#   Can't open /opt/ssl3/openssl.cnf for reading, No such file or directory
# The previous version of this script sidestepped that by calling the SYSTEM
# openssl (3.x, from apt) instead of the 1.1.1 build. We want the 1.1.1 build
# here -- it signs SHA-1 and 1024-bit RSA without argument, where OpenSSL 3 has
# opinions -- so supply the minimum config it needs.
OPENSSL_CONF=/certs/openssl.cnf
export OPENSSL_CONF
if [ ! -f "$OPENSSL_CONF" ]; then
    cat > "$OPENSSL_CONF" <<'EOF'
[req]
distinguished_name = dn
[dn]
EOF
fi

if [ ! -f "$CA_CRT" ]; then
    echo "generating PlayOnline revival CA (RSA 1024, SHA-1)"
    $SSL req -x509 -newkey rsa:1024 -nodes -sha1 \
        -keyout "$CA_KEY" -out "$CA_CRT" -days "$DAYS" \
        -subj "/C=US/O=PlayOnline Revival/OU=PlayOnline Revival Root CA" \
        -addext "basicConstraints=CA:TRUE" 2>/dev/null
    chmod 600 "$CA_KEY"
    # The server cert must be reissued from the new CA, so drop any old one.
    rm -f "$CERT" /certs/ssl3.key /certs/ssl3.crt
fi

if [ ! -f "$CERT" ]; then
    echo "issuing server cert for CN=$CN from the CA"
    $SSL req -newkey rsa:1024 -nodes -sha1 \
        -keyout /certs/ssl3.key -out /certs/ssl3.csr \
        -subj "/C=US/O=PlayOnline Revival/CN=$CN" 2>/dev/null
    # SAN covers the hosts the client actually dials; CN carries the wildcard
    # for any stack that only looks there.
    # Nothing marked critical, and no extendedKeyUsage. The roots this client
    # already trusts are X.509 v1 with no extensions at all, so the less it has
    # to understand in order to accept ours, the better.
    cat > /certs/ssl3.ext <<EOF
basicConstraints=CA:FALSE
subjectAltName=DNS:*.pol.com,DNS:pol.com,DNS:ucs.pol.com,DNS:userctl.pol.com,DNS:usercte.pol.com,DNS:*.playonline.com,DNS:*.square-enix.com,DNS:gate1.jp.dnas.playstation.org,DNS:*.jp.dnas.playstation.org,DNS:*.dnas.playstation.org
EOF
    $SSL x509 -req -in /certs/ssl3.csr -CA "$CA_CRT" -CAkey "$CA_KEY" \
        -CAcreateserial -out /certs/ssl3.crt -days "$DAYS" -sha1 \
        -extfile /certs/ssl3.ext 2>/dev/null
    # Key first, then the LEAF ONLY -- deliberately NOT the CA.
    #
    # We used to append the root, which made the server send a 2-certificate
    # chain [leaf, self-signed root]. That is wrong by the book (a root belongs
    # in the peer's trust store, not on the wire) and openssl flags the result
    # as "self signed certificate in certificate chain" (verify error 19). A
    # strict old stack can reject the chain on that alone, and the client's
    # behaviour matched: it read our Certificate message and closed without an
    # alert, which stunnel reports as `SSL_accept: Success (0)`.
    #
    # The client already has our root -- that is what tools/certstate.py
    # verifies -- so sending it again buys nothing and risks exactly this.
    cat /certs/ssl3.key /certs/ssl3.crt > "$CERT"
    chmod 600 "$CERT"
    echo "  issuer:  $($SSL x509 -in /certs/ssl3.crt -noout -issuer)"
    echo "  subject: $($SSL x509 -in /certs/ssl3.crt -noout -subject)"
    $SSL verify -CAfile "$CA_CRT" /certs/ssl3.crt || \
        echo "  WARNING: the chain does not verify against its own CA"
fi

# Which config to run. The image bakes three (stunnel.conf = the ssl3 default,
# stunnel-web.conf, stunnel-ucs.conf); production selects per-service with
# STUNNEL_CONF, while the dev compose keeps bind-mounting over the default.
CONF="${STUNNEL_CONF:-/etc/stunnel/stunnel.conf}"

# The baked configs name their backends by compose service ("connect = http:80",
# "connect = ucs-plain:8080"), which only resolves on a bridge network. Under
# `network_mode: host` (the production stack) there is no container DNS, so
# SSL3_CONNECT_HOST rewrites every connect target's HOST to the given address
# (ports are kept). Each config's sections all point at the same backend
# service, which is what makes a single blanket host substitution correct.
# The PS2's DNAS client on 443 rejects a cert whose hostname only appears in
# a SubjectAltName (its roots are X.509 v1, extension-blind), so it gets its
# own v1 leaf whose CN IS the hostname. stunnel-web.conf points its 443
# section at this file.
DNAS_CN="${SSL3_DNAS_CN:-gate1.jp.dnas.playstation.org}"
if [ ! -f /certs/dnas.pem ]; then
    echo "issuing DNAS cert for CN=$DNAS_CN (X.509 v1, CN-only)"
    $SSL req -newkey rsa:1024 -nodes -sha1 \
        -keyout /certs/dnas.key -out /certs/dnas.csr \
        -subj "/C=JP/O=PlayOnline Revival/CN=$DNAS_CN" 2>/dev/null
    # no -extfile: `x509 -req` without extensions emits a v1 certificate
    $SSL x509 -req -in /certs/dnas.csr -CA "$CA_CRT" -CAkey "$CA_KEY" \
        -CAcreateserial -out /certs/dnas.crt -days "$DAYS" -sha1 2>/dev/null
    cat /certs/dnas.key /certs/dnas.crt > /certs/dnas.pem
    chmod 600 /certs/dnas.pem
fi

if [ -n "${SSL3_CONNECT_HOST:-}" ]; then
    sed "s/^\( *connect *= *\)[^:]*:/\1${SSL3_CONNECT_HOST}:/" "$CONF" \
        > /tmp/stunnel.conf
    CONF=/tmp/stunnel.conf
    echo "connect hosts rewritten to ${SSL3_CONNECT_HOST}:"
    grep '^ *connect' "$CONF" | sed 's/^/    /'
fi

echo "ssl3 terminator: $($SSL version) (conf: ${STUNNEL_CONF:-/etc/stunnel/stunnel.conf})"
# Report the ciphers STUNNEL is configured with, resolved against this build.
# `openssl ciphers -ssl3` with no cipher string prints the default list (TLS 1.3
# suites and all), which looks reassuring and says nothing about what is served.
CIPHERS=$(sed -n 's/^ *ciphers *= *//p' "$CONF" | head -1)
if [ -n "$CIPHERS" ]; then
    echo "configured: $CIPHERS"
    echo "resolves to: $($SSL ciphers "$CIPHERS" 2>&1 | head -c 200)"
else
    echo "WARNING: no 'ciphers =' line in $CONF; SSLv3 will not negotiate"
fi

exec /opt/stunnel/bin/stunnel "$CONF"
