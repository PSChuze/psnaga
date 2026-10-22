#!/bin/sh
# fetch.sh FILE SHA256 URL... : download FILE from the first URL that gives the right checksum.
set -u
file=$1 sum=$2
shift 2
for url in "$@"; do
    for try in 1 2 3; do
        echo ">>> $file: $url (attempt $try)"
        if wget -nv --tries=1 --timeout=30 -O "$file" "$url" && echo "$sum  $file" | sha256sum -c -; then
            exit 0
        fi
        rm -f "$file"
        sleep $((try * 5))
    done
done
echo "ERROR: no mirror gave $file with the expected checksum"
exit 1
