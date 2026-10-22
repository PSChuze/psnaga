# The login's TLS terminator: TLS 1.0 / SSLv3 in front of nobu.auth (the GAMECITY login).
#
# The PS2 client sends an SSLv2-format ClientHello offering up to TLS 1.0 with 3DES / RC4 / DES
# and export suites. No current distribution can answer that: SSLv3 is gone from OpenSSL 3 and
# the 1.x packages are built without it. So OpenSSL 1.1.1w is built here with SSLv3 and the
# weak ciphers put back, and stunnel is linked against it.
#
# This is deliberately broken crypto, for one 2004 game client only. Put nothing else behind it.
#
# Accept / connect are set at run time (NOBU_SSL_ACCEPT, NOBU_SSL_CONNECT); the certificate is
# made on first start into /certs, so mount a volume there to keep it.
FROM debian:bookworm-slim AS build

ARG OPENSSL_VERSION=1.1.1w
ARG OPENSSL_TAG=OpenSSL_1_1_1w
ARG OPENSSL_SHA256=cf3098950cb4d853ad95c0841f1f9c6d3dc102dccfcacd521d93925208b76ac8
ARG STUNNEL_VERSION=5.80
ARG STUNNEL_SHA256=6d0841d48de07cbbaf4a055919065bf7bb5ebc63cc15c97a2c76caa2bf285513

RUN apt-get update && apt-get install -y --no-install-recommends build-essential wget ca-certificates perl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

# fetch URL... until one answers with the expected checksum
COPY fetch.sh /usr/local/bin/fetch.sh
RUN chmod +x /usr/local/bin/fetch.sh

RUN fetch.sh "openssl-${OPENSSL_VERSION}.tar.gz" "${OPENSSL_SHA256}" \
        "https://github.com/openssl/openssl/releases/download/${OPENSSL_TAG}/openssl-${OPENSSL_VERSION}.tar.gz" \
        "https://ftp.nluug.nl/security/openssl/openssl-${OPENSSL_VERSION}.tar.gz" \
        "https://www.openssl.org/source/old/1.1.1/openssl-${OPENSSL_VERSION}.tar.gz" \
    && tar xf "openssl-${OPENSSL_VERSION}.tar.gz" \
    && cd "openssl-${OPENSSL_VERSION}" \
    && ./config --prefix=/opt/ssl3 --openssldir=/opt/ssl3 \
        enable-ssl3 enable-ssl3-method enable-weak-ssl-ciphers no-shared no-tests \
    && make -j"$(nproc)" \
    && make install_sw \
    && /opt/ssl3/bin/openssl ciphers -ssl3 >/dev/null

# stunnel prunes /downloads/ to the current release; /archive/5.x/ keeps the old ones.
RUN fetch.sh "stunnel-${STUNNEL_VERSION}.tar.gz" "${STUNNEL_SHA256}" \
        "https://www.stunnel.org/archive/5.x/stunnel-${STUNNEL_VERSION}.tar.gz" \
        "https://ftp.nluug.nl/pub/networking/stunnel/archive/5.x/stunnel-${STUNNEL_VERSION}.tar.gz" \
    && tar xf "stunnel-${STUNNEL_VERSION}.tar.gz" \
    && cd "stunnel-${STUNNEL_VERSION}" \
    && ./configure --prefix=/opt/stunnel --with-ssl=/opt/ssl3 --disable-systemd \
    && make -j"$(nproc)" \
    && make install


FROM debian:bookworm-slim

COPY --from=build /opt/ssl3 /opt/ssl3
COPY --from=build /opt/stunnel /opt/stunnel
COPY entry.sh stunnel.conf req.cnf /conf/
RUN chmod +x /conf/entry.sh

VOLUME ["/certs"]
EXPOSE 9443

ENTRYPOINT ["/conf/entry.sh"]
