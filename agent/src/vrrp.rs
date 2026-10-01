//! Criterion 2 — VRRP state as seen by this gateway, and forced failover.
//! Responsible for: reading the state file written by Keepalived's notify.sh, creating/removing
//! the force-backup file that Keepalived's check_not_forced.sh watches.
//! NOT responsible for: VRRP itself (Keepalived does that) or HAProxy.

use std::io::ErrorKind;
use std::path::Path;

/// MASTER / BACKUP / FAULT, or UNKNOWN when Keepalived has not written anything yet.
pub fn read_state(state_file: &Path) -> String {
    match std::fs::read_to_string(state_file) {
        Ok(text) if !text.trim().is_empty() => text.trim().to_uppercase(),
        _ => "UNKNOWN".to_string(),
    }
}

pub fn is_forced_backup(force_file: &Path) -> bool {
    force_file.exists()
}

/// Creating the file makes check_not_forced.sh fail → priority −100 → the peer takes the VIP.
pub fn force_backup(force_file: &Path) -> std::io::Result<()> {
    if let Some(parent) = force_file.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(force_file, b"forced by controller\n")
}

/// Removing it is idempotent: clearing twice is fine.
pub fn clear_force_backup(force_file: &Path) -> std::io::Result<()> {
    match std::fs::remove_file(force_file) {
        Err(e) if e.kind() == ErrorKind::NotFound => Ok(()),
        other => other,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_state_file_is_unknown() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(read_state(&dir.path().join("nope")), "UNKNOWN");
    }

    #[test]
    fn state_is_trimmed_and_uppercased() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("vrrp_state");
        std::fs::write(&file, "master\n").unwrap();
        assert_eq!(read_state(&file), "MASTER");
    }

    #[test]
    fn force_and_clear_are_idempotent() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("gw/force_backup");
        force_backup(&file).unwrap();
        assert!(is_forced_backup(&file));
        clear_force_backup(&file).unwrap();
        clear_force_backup(&file).unwrap();
        assert!(!is_forced_backup(&file));
    }
}
