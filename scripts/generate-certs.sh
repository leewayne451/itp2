#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# ServiceJourney — generate the self-signed TLS material used by every service.
#
# Creates a small private Certificate Authority and one server certificate that
# is valid for BOTH the names browsers use (localhost) and the names containers
# use to reach each other on the docker network (backend, email, ...).
#
#   certs/ca.crt      the CA — containers trust this, you may import it into
#                     your browser to silence the warning
#   certs/ca.key      the CA private key (never leaves your machine)
#   certs/server.crt  the certificate every service presents
#   certs/server.key  its private key
#
# Re-run at any time to rotate; then: docker compose up -d --force-recreate
# ---------------------------------------------------------------------------
set -euo pipefail
export MSYS_NO_PATHCONV=1   # stop Git Bash rewriting the -subj argument

cd "$(dirname "$0")/.."
mkdir -p certs
cd certs

DAYS=825   # under the 825-day limit browsers enforce on leaf certificates

echo "==> Certificate Authority"
openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
  -keyout ca.key -out ca.crt \
  -subj "/O=ServiceJourney/CN=ServiceJourney Local Dev CA" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null

echo "==> Server key + signing request"
openssl req -newkey rsa:2048 -sha256 -nodes \
  -keyout server.key -out server.csr \
  -subj "/O=ServiceJourney/CN=localhost" 2>/dev/null

# Names the certificate must be valid for. The docker service names matter as
# much as localhost: container-to-container calls use https://backend:5000 etc.
cat > san.cnf <<'EOF'
subjectAltName = @alt
extendedKeyUsage = serverAuth
keyUsage = critical,digitalSignature,keyEncipherment
basicConstraints = CA:FALSE

[alt]
DNS.1 = localhost
DNS.2 = backend
DNS.3 = email
DNS.4 = accessmanagement
DNS.5 = service1
DNS.6 = service2
DNS.7 = postgres
IP.1  = 127.0.0.1
IP.2  = ::1
EOF

echo "==> Signing server certificate (valid ${DAYS} days)"
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out server.crt -days "$DAYS" -sha256 -extfile san.cnf 2>/dev/null

rm -f server.csr san.cnf ca.srl
chmod 600 ca.key server.key 2>/dev/null || true

echo
echo "Done. Subject alternative names on the server certificate:"
openssl x509 -in server.crt -noout -ext subjectAltName | sed 's/^/  /'
echo
openssl verify -CAfile ca.crt server.crt | sed 's/^/  /'
