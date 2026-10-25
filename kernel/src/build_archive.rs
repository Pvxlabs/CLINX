use super::*;
use flate2::{Compression, GzBuilder, read::GzDecoder};
pub fn archive(root: &Path, target: &Path) -> Result<()> {
    let output = fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(target)
        .map_err(|e| e.to_string())?;
    let gzip = GzBuilder::new().mtime(0).write(output, Compression::best());
    let mut tar = tar::Builder::new(gzip);
    for name in walk(root)? {
        let p = root.join(&name);
        let bytes = read(&p)?;
        let mut h = tar::Header::new_ustar();
        h.set_path(&name).map_err(|e| e.to_string())?;
        h.set_mode(regular(&p)?.mode() & 0o7777);
        h.set_size(bytes.len() as u64);
        h.set_mtime(0);
        h.set_uid(0);
        h.set_gid(0);
        h.set_entry_type(tar::EntryType::Regular);
        h.set_cksum();
        tar.append(&h, &bytes[..]).map_err(|e| e.to_string())?;
    }
    let gzip = tar.into_inner().map_err(|e| e.to_string())?;
    gzip.finish().map_err(|e| e.to_string())?;
    Ok(())
}
fn reader(bytes: &[u8]) -> Box<dyn Read + '_> {
    if bytes.starts_with(&[31, 139]) {
        Box::new(GzDecoder::new(bytes))
    } else {
        Box::new(bytes)
    }
}
pub fn extract(path: &Path, destination: &Path, expected: &str) -> Result<()> {
    let bytes = read(path)?;
    if sha(&bytes) != expected {
        return Err("PINNED_ARCHIVE_HASH_MISMATCH".into());
    }
    let mut names = BTreeSet::new();
    {
        let mut archive = tar::Archive::new(reader(&bytes));
        let mut global_comment_seen = false;
        for e in archive.entries().map_err(|e| e.to_string())? {
            let mut e = e.map_err(|e| format!("ARCHIVE_NON_REGULAR_OR_TRAVERSAL:{e}"))?;
            let name = e.path().map_err(|_| "ARCHIVE_NON_REGULAR_OR_TRAVERSAL")?;
            if name.is_absolute() || name.components().any(|c| matches!(c, Component::ParentDir)) {
                return Err("ARCHIVE_NON_REGULAR_OR_TRAVERSAL".into());
            }
            // GitHub's pinned source archive starts with a global PAX commit
            // comment. Python tarfile hid it; tar exposes this metadata entry.
            // Admit only that inert comment, never global path/link overrides,
            // and never materialize the metadata as an extracted file.
            if e.header().entry_type().is_pax_global_extensions() {
                if global_comment_seen || e.size() != 52 {
                    return Err("ARCHIVE_GLOBAL_PAX_REFUSED".into());
                }
                let mut comment = [0; 52];
                e.read_exact(&mut comment)
                    .map_err(|_| "ARCHIVE_GLOBAL_PAX_REFUSED")?;
                if !comment.starts_with(b"52 comment=")
                    || comment[51] != b'\n'
                    || !comment[11..51]
                        .iter()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
                {
                    return Err("ARCHIVE_GLOBAL_PAX_REFUSED".into());
                }
                global_comment_seen = true;
                continue;
            }
            if !(e.header().entry_type().is_file() || e.header().entry_type().is_dir())
                || !names.insert(name.to_path_buf())
            {
                return Err("ARCHIVE_NON_REGULAR_OR_TRAVERSAL".into());
            }
        }
    }
    fs::create_dir_all(destination).map_err(|e| e.to_string())?;
    let mut archive = tar::Archive::new(reader(&bytes));
    for e in archive.entries().map_err(|e| e.to_string())? {
        let mut e = e.map_err(|e| e.to_string())?;
        if e.header().entry_type().is_pax_global_extensions() {
            continue;
        }
        if !e.unpack_in(destination).map_err(|e| e.to_string())? {
            return Err("ARCHIVE_NON_REGULAR_OR_TRAVERSAL".into());
        }
    }
    Ok(())
}
pub fn remove_transient(path: &Path) -> Result<()> {
    if !path.exists() {
        return Ok(());
    }
    fn chmod(p: &Path) -> Result<()> {
        let m = fs::symlink_metadata(p).map_err(|e| e.to_string())?;
        if m.is_dir() {
            fs::set_permissions(p, fs::Permissions::from_mode(0o700)).map_err(|e| e.to_string())?;
            for e in fs::read_dir(p).map_err(|e| e.to_string())? {
                chmod(&e.map_err(|e| e.to_string())?.path())?
            }
        }
        Ok(())
    }
    chmod(path)?;
    fs::remove_dir_all(path).map_err(|e| e.to_string())
}
