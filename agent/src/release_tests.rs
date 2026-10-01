//! Tests for release.rs (kept in their own file so release.rs stays short).
//! Covers: applied, rejected leaves symlink untouched, probe failure rolls back, unchanged,
//! bad checksum, prune keeps active/previous. Uses MockHaproxy.
//! Serves Criterion 4b.

use super::*;
use crate::haproxy::mock::MockHaproxy;
use std::path::Path;

struct Setup {
    _dir: tempfile::TempDir,
    mock: Arc<MockHaproxy>,
    manager: ReleaseManager,
    link: PathBuf,
    releases: PathBuf,
}

/// Same starting point as the gateway entrypoint: v0.cfg installed and current.cfg → v0.cfg.
fn setup(keep_releases: usize) -> Setup {
    let dir = tempfile::tempdir().unwrap();
    let releases = dir.path().join("releases");
    std::fs::create_dir_all(&releases).unwrap();
    std::fs::write(releases.join("v0.cfg"), "config 0").unwrap();
    let link = dir.path().join("current.cfg");
    std::os::unix::fs::symlink(releases.join("v0.cfg"), &link).unwrap();

    let mock = Arc::new(MockHaproxy::new(link.clone()));
    *mock.served_version.lock().unwrap() = Some("0".into());
    let settings = ReleaseSettings {
        releases_dir: releases.clone(),
        current_link: link.clone(),
        probe_timeout: Duration::from_millis(200),
        probe_interval: Duration::from_millis(5),
        keep_releases,
    };
    let manager = ReleaseManager::new(mock.clone(), settings);
    Setup { _dir: dir, mock, manager, link, releases }
}

fn request(version: u64, config: &str) -> ConfigRequest {
    ConfigRequest { version, config: config.to_string(), checksum: sha256_hex(config) }
}

fn link_target(link: &Path) -> String {
    std::fs::read_link(link).unwrap().file_name().unwrap().to_str().unwrap().to_string()
}

#[tokio::test]
async fn valid_config_is_applied() {
    let s = setup(20);
    assert_eq!(s.manager.state().active, Some(0), "state derived from symlink at startup");
    let result = s.manager.apply(request(1, "config 1")).await.unwrap();
    assert_eq!(result.outcome, Some(Outcome::Applied));
    assert_eq!(link_target(&s.link), "v1.cfg");
    assert_eq!(s.manager.state(), ReleaseState { active: Some(1), previous: Some(0) });
    let saved = std::fs::read_to_string(s.releases.join("state.json")).unwrap();
    assert!(saved.contains("\"active\":1"));
}

#[tokio::test]
async fn rejected_config_leaves_symlink_untouched() {
    let s = setup(20);
    let result = s.manager.apply(request(1, "this is not haproxy {{{")).await.unwrap();
    assert_eq!(result.outcome, Some(Outcome::Rejected));
    assert!(result.error.is_some());
    assert_eq!(link_target(&s.link), "v0.cfg");
    assert!(!s.releases.join("v1.cfg").exists(), "rejected file is deleted");
    assert_eq!(*s.mock.reloads.lock().unwrap(), 0, "no reload for a rejected config");
}

#[tokio::test]
async fn probe_failure_rolls_back_to_previous() {
    let s = setup(20);
    s.manager.apply(request(1, "config 1")).await.unwrap();
    s.mock.broken_versions.lock().unwrap().insert("2".into());

    let result = s.manager.apply(request(2, "config 2 binds the wrong port")).await.unwrap();
    assert_eq!(result.outcome, Some(Outcome::RolledBack));
    assert_eq!(link_target(&s.link), "v1.cfg");
    assert_eq!(result.active_version, Some(1));
    assert!(result.rollback_ms < 1000);
    assert_eq!(s.manager.state(), ReleaseState { active: Some(1), previous: Some(0) });
    assert_eq!(s.mock.served_version.lock().unwrap().as_deref(), Some("1"));
}

#[tokio::test]
async fn same_version_and_checksum_is_unchanged() {
    let s = setup(20);
    s.manager.apply(request(1, "config 1")).await.unwrap();
    let result = s.manager.apply(request(1, "config 1")).await.unwrap();
    assert_eq!(result.outcome, Some(Outcome::Unchanged));
    assert_eq!(*s.mock.reloads.lock().unwrap(), 1);
}

#[tokio::test]
async fn wrong_checksum_is_a_bad_request() {
    let s = setup(20);
    let mut bad = request(1, "config 1");
    bad.checksum = "0000".into();
    assert!(matches!(s.manager.apply(bad).await, Err(AgentError::BadRequest(_))));
}

#[tokio::test]
async fn prune_keeps_active_and_previous() {
    let s = setup(2);
    for version in 1..=5 {
        let result = s.manager.apply(request(version, &format!("config {version}"))).await.unwrap();
        assert_eq!(result.outcome, Some(Outcome::Applied));
    }
    let mut left = list_release_versions(&s.releases);
    left.sort_unstable();
    assert_eq!(left, vec![4, 5]);
    assert_eq!(s.manager.state(), ReleaseState { active: Some(5), previous: Some(4) });
}

#[tokio::test]
async fn manual_rollback_returns_to_previous() {
    let s = setup(20);
    s.manager.apply(request(1, "config 1")).await.unwrap();
    let result = s.manager.rollback_to_previous().await.unwrap();
    assert_eq!(result.outcome, Some(Outcome::Applied));
    assert_eq!(link_target(&s.link), "v0.cfg");
    assert_eq!(s.manager.state(), ReleaseState { active: Some(0), previous: Some(1) });
}
