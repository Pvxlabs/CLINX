//! Install the independent native workspace oracle into a disposable directory.
pub fn install(root: &std::path::Path, name: &str) -> std::path::PathBuf {
    let source = std::env::current_exe()
        .unwrap()
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .join("examples/qualification_workspace");
    assert!(
        source.is_file(),
        "build qualification examples first: cargo build --examples ({})",
        source.display()
    );
    let destination = root.join(name);
    std::fs::copy(source, &destination).unwrap();
    destination
}
