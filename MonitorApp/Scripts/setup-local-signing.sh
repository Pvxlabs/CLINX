#!/bin/sh
# Run once on a development Mac. Never commit/export the generated private key.
set -eu
identity='CLINX Monitor Local Development'
if security find-certificate -c "$identity" >/dev/null 2>&1; then
    echo 'Local signing certificate already exists; keeping its identity.'
    exit 0
fi
umask 077
signing_tmp=$(mktemp -d "${TMPDIR:-/tmp}/clinx-signing.XXXXXX")
trap 'rm -rf "$signing_tmp"' EXIT HUP INT TERM
cat > "$signing_tmp/certificate.cnf" <<'CONFIG'
[req]
prompt = no
distinguished_name = subject
x509_extensions = extensions
[subject]
CN = CLINX Monitor Local Development
[extensions]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
CONFIG
openssl req -new -newkey rsa:3072 -nodes -x509 -days 3650 \
    -config "$signing_tmp/certificate.cnf" \
    -keyout "$signing_tmp/key.pem" -out "$signing_tmp/certificate.pem" 2>/dev/null
signing_pass=$(openssl rand -hex 24)
printf '%s' "$signing_pass" > "$signing_tmp/passphrase"
openssl pkcs12 -export -inkey "$signing_tmp/key.pem" -in "$signing_tmp/certificate.pem" \
    -name "$identity" -out "$signing_tmp/identity.p12" -passout "file:$signing_tmp/passphrase"
# Limit private-key access to the signing tool. No all-app ACL or global trust change.
security import "$signing_tmp/identity.p12" -f pkcs12 -P "$signing_pass" -x -T /usr/bin/codesign
printf '%s\n' 'Local signing identity imported into the default Keychain.'
