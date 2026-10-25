#!/bin/bash
# One-time privileged installation glue. Runtime/maintenance logic stays in Rust.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
unset BASH_ENV ENV CDPATH
umask 077
[[ $EUID == 0 && $# == 5 ]] || { echo 'usage: sudo bash install-clinx-admin.sh BUNDLE ADMIN_SHA256 MAINTENANCE_SHA256 CURRENT_RELEASE MANIFEST_SHA256' >&2; exit 1; }
bundle=$1
admin_hash=$2
maintenance_hash=$3
release_id=$4
manifest_hash=$5
[[ $bundle == /data/artifacts/* && $admin_hash =~ ^[a-f0-9]{64}$ && $maintenance_hash =~ ^[a-f0-9]{64}$ && $release_id =~ ^rust-[a-f0-9]{20}$ && $manifest_hash =~ ^[a-f0-9]{64}$ ]] || exit 1
current=/var/lib/clinx-rust/releases/$release_id

# Refuse writable/symlinked installation ancestors before making changes.
for dir in / /usr /usr/local /usr/local/sbin /etc /etc/sudoers.d /var /var/lib /var/lib/clinx-rust /var/lib/clinx-rust/releases; do
    [[ -d $dir && ! -L $dir && $(stat -c %u "$dir") == 0 ]] || { echo "UNTRUSTED_INSTALL_PATH:$dir" >&2; exit 1; }
    mode=$(stat -c %a "$dir")
    (( (8#$mode & 0022) == 0 )) || exit 1
done
visudo -c
for dir in /usr/local/libexec /usr/local/libexec/clinx-admin-tools /var/lib/clinx-rust/admin; do
    [[ ! -L $dir ]] || exit 1
    if [[ -e $dir ]]; then
        [[ -d $dir && $(stat -c %u "$dir") == 0 ]] || exit 1
        mode=$(stat -c %a "$dir")
        (( (8#$mode & 0022) == 0 )) || exit 1
    fi
done
install -d -o root -g root -m 0755 /usr/local/libexec /usr/local/libexec/clinx-admin-tools /var/lib/clinx-rust/admin
exec 9>/var/lib/clinx-rust/admin/operator.lock
flock -n 9 || { echo 'ADMIN_OPERATION_IN_PROGRESS' >&2; exit 1; }
saved=$(mktemp -d /var/lib/clinx-rust/admin/bootstrap-XXXXXXXX)
chmod 0700 "$saved"
printf '%s\n' "$saved"

# Seal the known active release's inherited operator-write ACLs. This changes
# artifact permissions only; do not stop services or touch canonical state.
selected=''
while IFS= read -r line; do
    if [[ $line == ExecStart=* ]]; then selected=${line%% *}; fi
done < /etc/systemd/system/clinx-rust-authority.service
[[ $selected == "ExecStart=$current/clinx-runtime" && -d $current && ! -L $current && $(stat -c %u "$current") == 0 ]] || exit 1
getfacl -R -p "$current" > "$saved/release-acl.before"
# Preorder walk seals a parent before examining children; never follow links.
find -P "$current" -type d -exec chmod 0755 -- {} \;
[[ -z $(find -P "$current" \( -type l -o ! -uid 0 \) -print -quit) ]] || { echo 'RELEASE_OWNERSHIP_CONFLICT' >&2; exit 1; }
printf '%s  %s\n' "$manifest_hash" "$current/manifest.json" | sha256sum -c -
setfacl -R -P -b -k "$current"
setfacl -k /var/lib/clinx-rust/releases
printf '%s  %s\n' "$manifest_hash" "$current/manifest.json" | sha256sum -c -

# Read operator-owned candidates with operator privileges, then hash the fixed
# root snapshot. Neither candidate is executed during installation.
runuser -u pvxlabs -- /usr/bin/cat -- "$bundle/clinx-admin" > "$saved/clinx-admin.new"
runuser -u pvxlabs -- /usr/bin/cat -- "$bundle/clinx-maintenance" > "$saved/clinx-maintenance.new"
printf '%s  %s\n' "$admin_hash" "$saved/clinx-admin.new" "$maintenance_hash" "$saved/clinx-maintenance.new" | sha256sum -c -
cat > "$saved/sudoers.new" <<EOF
# CLINX only. Authorization for each task remains required.
Defaults!/usr/local/sbin/clinx-admin env_reset, !setenv, secure_path="/usr/sbin:/usr/bin:/sbin:/bin"
pvxlabs ALL=(root) NOPASSWD: NOSETENV: sha256:$admin_hash /usr/local/sbin/clinx-admin
EOF
chmod 0440 "$saved/sudoers.new"
visudo -cf "$saved/sudoers.new"

destinations=(/usr/local/sbin/clinx-admin /usr/local/libexec/clinx-admin-tools/clinx-maintenance /etc/sudoers.d/90-p620-clinx-admin)
sources=(clinx-admin.new clinx-maintenance.new sudoers.new)
modes=(0755 0755 0440)
for i in 0 1 2; do
    p=${destinations[$i]}
    [[ ! -L $p ]] || exit 1
    if [[ -e $p ]]; then
        [[ -f $p && $(stat -c %u "$p") == 0 ]] || exit 1
        cp -a -- "$p" "$saved/previous-$i"
    fi
done
rollback() {
    code=$?
    trap - ERR
    for i in 0 1 2; do
        p=${destinations[$i]}
        if [[ -e $saved/previous-$i ]]; then
            cp -a -- "$saved/previous-$i" "$p"
        else
            rm -f -- "$p"
        fi
    done
    visudo -c || true
    echo "INSTALL_FAILED_BACKUP:$saved" >&2
    exit "$code"
}
trap rollback ERR
for i in 0 1 2; do
    p=${destinations[$i]}
    pending=$(mktemp "$(dirname "$p")/.clinx-install-XXXXXXXX")
    install -o root -g root -m "${modes[$i]}" "$saved/${sources[$i]}" "$pending"
    mv -Tf -- "$pending" "$p"
done
visudo -c
# Release the shared lock before the read-only Rust self-check takes it.
flock -u 9
runuser -u pvxlabs -- sudo -n /usr/local/sbin/clinx-admin status > "$saved/status.json"
runuser -u pvxlabs -- sudo -n -l > "$saved/sudo-list.txt"
trap - ERR
sync -f "$saved"
echo "CLINX_ADMIN_INSTALL=PASS receipts=$saved"
# No service lifecycle, database write, pairing or release selection here.
