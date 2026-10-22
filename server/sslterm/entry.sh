#!/bin/sh
# Self-signed, v1 (no extensions), RSA-1024, SHA-1 leaf with CN = the login host.
# NOBU_SSL_ACCEPT / NOBU_SSL_CONNECT override stunnel.conf's accept / connect lines (default
# 0.0.0.0:9443 and auth:18080, the compose service).
set -eu
SSL=/opt/ssl3/bin/openssl
if [ ! -f /certs/login.pem ]; then
  $SSL genrsa -out /certs/login.key 1024
  $SSL req -new -key /certs/login.key -sha1 -config /conf/req.cnf -out /certs/login.csr
  $SSL x509 -req -in /certs/login.csr -signkey /certs/login.key -sha1 -days 3650 -out /certs/login.crt
  cat /certs/login.key /certs/login.crt > /certs/login.pem
fi
$SSL x509 -in /certs/login.crt -noout -text | grep -E "Version|Subject:|Signature Algorithm|Public-Key" | head -4
CONF=/conf/stunnel.conf
if [ -n "${NOBU_SSL_ACCEPT:-}${NOBU_SSL_CONNECT:-}${NOBU_SSL_DEBUG:-}" ]; then
  sed -e "${NOBU_SSL_ACCEPT:+s|^accept .*|accept  = $NOBU_SSL_ACCEPT|}" \
      -e "${NOBU_SSL_CONNECT:+s|^connect .*|connect = $NOBU_SSL_CONNECT|}" \
      -e "${NOBU_SSL_DEBUG:+s|^debug .*|debug = $NOBU_SSL_DEBUG|}" \
      /conf/stunnel.conf > /tmp/stunnel.conf
  CONF=/tmp/stunnel.conf
fi
exec /opt/stunnel/bin/stunnel "$CONF"
